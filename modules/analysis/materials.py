"""Fixed original reads and explicit material-only capture for a failed call."""

import hashlib
from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Protocol

from modules.analysis.application.documents import DocumentInterpretationError
from modules.analysis.application.documents import DocumentNotFoundError
from modules.analysis.application.documents import DocumentParsingError
from modules.analysis.preparation import payload_digest
from modules.analysis.snapshots import MATERIAL_CONTRACT
from modules.analysis.snapshots import DocumentScope
from modules.analysis.snapshots import DocumentSnapshot
from modules.analysis.snapshots import canonical_json
from modules.analysis.understanding import DocumentSnapshotChangedError


class DocumentMaterialUnavailableError(DocumentNotFoundError):
    code = 'document.material_unavailable'
    retryable = False


class DocumentMaterialIntegrityError(RuntimeError):
    code = 'document.material_integrity'
    retryable = False
    content_failure = False


@dataclass(frozen=True, slots=True)
class DocumentSourceCall:
    entrypoint: str
    source_id: str
    session_id: str
    file_index: int
    requested_document_id: str | None
    file_url: str | None
    started_at: datetime
    trace_id: str | None = None

    def __post_init__(self):
        if any(
            not isinstance(v, str) or not v or v.strip() != v
            for v in (self.entrypoint, self.source_id, self.session_id)
        ):
            raise ValueError('the complete source call identity is required')
        if type(self.file_index) is not int or self.file_index < 0:
            raise ValueError('file_index must be a nonnegative integer')
        if not isinstance(self.started_at, datetime) or self.started_at.utcoffset() is None:
            raise ValueError('started_at must be a timezone-aware timestamp')
        if any(
            v is not None and (not isinstance(v, str) or not v or v.strip() != v)
            for v in (self.requested_document_id, self.file_url, self.trace_id)
        ):
            raise ValueError('optional source references must be nonempty strings')
        if self.requested_document_id is None and self.file_url is None:
            raise ValueError('an exact document or file URL reference is required')

    def source(self):
        return {
            'entrypoint': self.entrypoint,
            'sourceId': self.source_id,
            'sessionId': self.session_id,
            'fileIndex': self.file_index,
        }

    def as_dict(self):
        return {**asdict(self), 'started_at': self.started_at.astimezone(timezone.utc).isoformat()}

    @property
    def mongo_started_at(self):
        """Match the precision MongoDB uses for both BSON dates and queries."""
        value = self.started_at.astimezone(timezone.utc)
        return value.replace(microsecond=value.microsecond // 1000 * 1000)


class DocumentMaterialArchive(Protocol):
    async def locate_failed(
        self, scope: DocumentScope, call: DocumentSourceCall, *, stage='interpreting'
    ) -> dict | None: ...
    async def get_trace(self, scope: DocumentScope, trace_id: str) -> dict: ...
    async def read_payload(self, scope: DocumentScope, trace_id: str, name: str): ...


def _error_chain(error):
    seen, chain = set(), []
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        chain.append(error)
        error = error.__cause__
    return chain


def _parsing_failure(error):
    chain = _error_chain(error)
    if any(
        getattr(item, 'content_failure', True) is False
        or isinstance(item, PermissionError)
        or getattr(item, 'status_code', None) in (401, 403)
        for item in chain
    ):
        return None
    return next((item for item in chain if isinstance(item, DocumentParsingError)), None)


def _aware(value):
    # PyMongo's default codec returns naive UTC timestamps.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class DocumentMaterialReader:
    def __init__(self, *, snapshots, archive: DocumentMaterialArchive):
        self.snapshots = snapshots
        self.archive = archive

    async def read_material(self, *, scope: DocumentScope, material_snapshot_ref: str) -> DocumentSnapshot:
        if not isinstance(scope, DocumentScope):
            raise TypeError('DocumentScope is required')
        snapshot = await self.snapshots.get(scope, material_snapshot_ref)
        if snapshot.contract_version != MATERIAL_CONTRACT:
            raise ValueError('an exact material snapshot is required')
        return snapshot

    @staticmethod
    def _original_identity(snapshot):
        payload = snapshot.payload
        metadata = payload.get('documentMetadata', {})
        ref = payload.get('originalArtifactRef')
        size = metadata.get('byteSize', payload.get('byteSize'))
        if (
            not isinstance(ref, str)
            or not ref
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 1
            or not isinstance(payload.get('contentSha256'), str)
            or len(payload['contentSha256']) != 64
            or not payload.get('documentId')
        ):
            raise DocumentMaterialUnavailableError('fixed original reference, checksum and byte size are required')
        if metadata and (
            metadata.get('documentId') != payload['documentId']
            or metadata.get('canonicalReportId') != snapshot.source_id
            or metadata.get('contentSha256') != payload['contentSha256']
            or metadata.get('originalArtifactRef') != ref
        ):
            raise DocumentMaterialIntegrityError('material source identity conflicts')
        return ref, size

    @staticmethod
    def _trace_owner(snapshot, trace):
        if (
            trace.get('userId') != snapshot.scope.user_id
            or trace.get('subjectId') != snapshot.scope.subject_id
            or trace.get('documentId') != snapshot.payload['documentId']
            or trace.get('canonicalReportId') != snapshot.source_id
        ):
            raise DocumentMaterialIntegrityError('original trace is outside the fixed source identity')

    async def read_original(self, *, scope: DocumentScope, material_snapshot_ref: str) -> bytes:
        snapshot = await self.read_material(scope=scope, material_snapshot_ref=material_snapshot_ref)
        return await self._validated_original(snapshot)

    async def _validated_original(self, snapshot):
        scope = snapshot.scope
        ref, size = self._original_identity(snapshot)
        trace = await self.archive.get_trace(scope, ref)
        self._trace_owner(snapshot, trace)
        raw = await self.archive.read_payload(scope, ref, 'original_file')
        content = raw.get('content') if isinstance(raw, dict) else None
        if (
            not isinstance(content, bytes)
            or len(content) != size
            or hashlib.sha256(content).hexdigest() != snapshot.payload['contentSha256']
        ):
            raise DocumentMaterialIntegrityError('original content differs from the selected material')
        current = await self.archive.get_trace(scope, ref)
        self._trace_owner(snapshot, current)
        return content

    @staticmethod
    def _eligible_trace(scope, call, trace, *, stage='interpreting'):
        created = trace.get('createdAt')
        return (
            trace.get('userId') == scope.user_id
            and trace.get('subjectId') == scope.subject_id
            and trace.get('source') == call.source()
            and trace.get('requestedDocumentId') == call.requested_document_id
            and trace.get('fileUrl') == call.file_url
            and isinstance(created, datetime)
            and _aware(created) >= call.mongo_started_at
            and (call.trace_id is None or trace.get('_id') == call.trace_id)
            and trace.get('status') == 'failed'
            and trace.get('stage') == stage
            and trace.get('errorType') in {DocumentParsingError.__name__, DocumentInterpretationError.__name__}
            and trace.get('traceId') == trace.get('_id')
            and bool(trace.get('documentId'))
        )

    async def capture_failed_material(self, *, scope: DocumentScope, call: DocumentSourceCall, failure: BaseException):
        if not isinstance(scope, DocumentScope) or not isinstance(call, DocumentSourceCall):
            raise TypeError('explicit DocumentScope and DocumentSourceCall are required')
        parsing_failure = _parsing_failure(failure)
        if parsing_failure is None:
            return None
        trace = await self.archive.locate_failed(scope, call)
        if trace is None or not self._eligible_trace(scope, call, trace):
            return None
        if any(
            getattr(item, 'trace_id', None) not in (None, trace['_id'])
            or getattr(item, 'document_id', None) not in (None, trace['documentId'])
            for item in _error_chain(failure)
        ):
            return None
        binding = trace.get('failureMaterialBinding')
        if not isinstance(binding, dict):
            raise DocumentMaterialUnavailableError('legacy failure trace has no fixed material binding')
        frozen = {key: value for key, value in binding.items() if key != 'bindingDigest'}
        metadata = binding.get('documentMetadata', {})
        if (
            payload_digest(frozen) != binding.get('bindingDigest')
            or binding.get('scope') != scope.as_dict()
            or metadata.get('documentId') != trace['documentId']
            or metadata.get('canonicalReportId') != trace.get('canonicalReportId')
            or not (
                metadata.get('status') == 'failed'
                and binding.get('errorType') in {'DocumentParsingError', 'DocumentInterpretationError'}
                or metadata.get('status') == 'completed'
                and binding.get('processingStatus') == 'unavailable'
            )
        ):
            raise DocumentMaterialIntegrityError('failure binding does not identify this failed call')
        parsed = await self.archive.read_payload(scope, binding['parseArtifactRef'], 'docmind_result')
        if payload_digest(parsed) != binding['parseContentDigest']:
            raise DocumentMaterialIntegrityError('fixed parser content changed after failure')
        snapshot = DocumentSnapshot(
            scope,
            MATERIAL_CONTRACT,
            metadata['canonicalReportId'],
            1,
            canonical_json(
                {
                    'documentId': metadata['documentId'],
                    'contentSha256': metadata['contentSha256'],
                    'fileName': metadata['fileName'],
                    'fileType': metadata['fileType'],
                    'parserRevision': metadata['parserRevision'],
                    'originalArtifactRef': binding['originalArtifactRef'],
                    'parseArtifactRef': binding['parseArtifactRef'],
                    'parsedDocument': parsed,
                    'documentMetadata': metadata,
                    'interpretationRevision': binding['interpretationRevision'],
                    'validationRevision': binding['validationRevision'],
                    'interpretationRound': binding['interpretationRound'],
                    'interpretationResultRound': binding['interpretationResultRound'],
                    'failureTrace': {
                        'traceId': trace['traceId'],
                        'errorType': trace['errorType'],
                        'status': trace['status'],
                        'stage': trace['stage'],
                        'call': call.as_dict(),
                        'bindingDigest': binding['bindingDigest'],
                    },
                }
            ),
        )
        parsed_trace = await self.archive.get_trace(scope, binding['parseArtifactRef'])
        self._trace_owner(snapshot, parsed_trace)
        # Verify the exact original without first publishing a material reference.
        await self._validated_original(snapshot)
        current = await self.archive.get_trace(scope, trace['_id'])
        if not self._eligible_trace(scope, call, current) or current.get('failureMaterialBinding') != binding:
            raise DocumentSnapshotChangedError('failure trace changed during material capture')
        current_parsed = await self.archive.read_payload(scope, binding['parseArtifactRef'], 'docmind_result')
        if payload_digest(current_parsed) != binding['parseContentDigest']:
            raise DocumentSnapshotChangedError('parser archive changed during material capture')
        await self.snapshots.put(snapshot)
        return snapshot

    async def capture_failed_original(self, *, scope: DocumentScope, call: DocumentSourceCall, failure: BaseException):
        """Freeze the exact already verified PDF when OCR polling fails before a parse exists."""
        if not isinstance(scope, DocumentScope) or not isinstance(call, DocumentSourceCall):
            raise TypeError('explicit DocumentScope and DocumentSourceCall are required')
        parsing_failure = _parsing_failure(failure)
        if (
            call.entrypoint != 'report'
            or parsing_failure is None
            or parsing_failure.reason_code != 'ocr_query_failed'
            or call.trace_id is None
            or getattr(parsing_failure, 'trace_id', None) != call.trace_id
        ):
            return None
        trace = await self.archive.locate_failed(scope, call, stage='polling')
        if (
            trace is None
            or not self._eligible_trace(scope, call, trace, stage='polling')
            or trace.get('reasonCode') != 'ocr_query_failed'
            or getattr(parsing_failure, 'document_id', None) != trace['documentId']
        ):
            return None
        proof = trace.get('verifiedOriginal')
        if not isinstance(proof, dict) or proof.get('fileType') != 'pdf':
            return None
        if not isinstance(proof.get('fileName'), str) or not proof['fileName'] or not trace.get('canonicalReportId'):
            raise DocumentMaterialIntegrityError('fixed failed PDF has no complete source identity')
        if call.file_url is not None and (
            trace.get('contentSha256') != proof.get('contentSha256') or trace.get('byteSize') != proof.get('byteSize')
        ):
            raise DocumentMaterialIntegrityError('fixed failed PDF differs from the downloaded file')
        snapshot = DocumentSnapshot(
            scope,
            MATERIAL_CONTRACT,
            trace['canonicalReportId'],
            1,
            canonical_json(
                {
                    'documentId': trace['documentId'],
                    'contentSha256': proof.get('contentSha256'),
                    'fileName': proof['fileName'],
                    'fileType': proof['fileType'],
                    'originalArtifactRef': proof.get('artifactRef'),
                    'byteSize': proof.get('byteSize'),
                    'failureTrace': {
                        'traceId': trace['traceId'],
                        'errorType': trace['errorType'],
                        'status': trace['status'],
                        'stage': trace['stage'],
                        'call': call.as_dict(),
                    },
                }
            ),
        )
        await self._validated_original(snapshot)
        current = await self.archive.get_trace(scope, call.trace_id)
        if (
            not self._eligible_trace(scope, call, current, stage='polling')
            or current.get('verifiedOriginal') != proof
            or current.get('canonicalReportId') != snapshot.source_id
            or current.get('documentId') != trace['documentId']
            or current.get('reasonCode') != trace['reasonCode']
        ):
            raise DocumentSnapshotChangedError('failure trace changed during original capture')
        await self.snapshots.put(snapshot)
        return snapshot
