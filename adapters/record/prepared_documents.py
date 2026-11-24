"""Consume immutable Document bundles through a Record-owned renderer.

The publisher is injected through a Record port. Production can select this
adapter only after creation recovery persists the prepared checkpoint first.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Mapping
from typing import Any
from typing import Protocol

from modules.analysis.contracts import OutputSelection
from modules.analysis.snapshots import DocumentScope
from modules.analysis.understanding import DocumentPreparedBundle
from modules.record.documents import LEGACY_RECORD_DOCUMENT_CONSUMER
from modules.record.documents import RECORD_DOCUMENT_CHECKPOINT
from modules.record.documents import RECORD_UNDERSTANDING_SELECTION
from modules.record.documents import RecordDocumentCheckpointError
from modules.record.documents import RecordDocumentPublication
from modules.record.documents import RecordDocumentVersionError
from modules.record.documents import RecordPreparedDocumentContextV1
from modules.record.documents import RecordPreparedDocumentPublisher
from modules.record.documents import RecordPreparedDocumentV1
from modules.record.documents import render_record_document_v1

_RECORD_SELECTION = OutputSelection(*RECORD_UNDERSTANDING_SELECTION)


class RecordDocumentBundlePort(Protocol):
    async def prepare_bundle(self, *, scope, selection, **kwargs) -> DocumentPreparedBundle: ...
    async def read_bundle(self, *, scope, understanding_snapshot_id, selection) -> DocumentPreparedBundle: ...


class PreparedRecordDocumentAdapter:
    prepared_checkpoint_version = RECORD_DOCUMENT_CHECKPOINT
    prepared_checkpoint_type = RecordPreparedDocumentV1
    preparation_selection = {'selection': _RECORD_SELECTION}

    def __init__(
        self,
        *,
        understanding: RecordDocumentBundlePort,
        publisher: RecordPreparedDocumentPublisher,
        ensure_self_scope: Callable[[str], Awaitable[DocumentScope]],
        legacy_port=None,
    ) -> None:
        self.understanding = understanding
        self.publisher = publisher
        self.ensure_self_scope = ensure_self_scope
        self.legacy_port = legacy_port

    def for_consumer_revision(self, revision):
        if revision == RECORD_DOCUMENT_CHECKPOINT:
            return self
        if revision == LEGACY_RECORD_DOCUMENT_CONSUMER and self.legacy_port is not None:
            return self.legacy_port
        raise RecordDocumentVersionError('the accepted Record document consumer is unavailable')

    @classmethod
    def _freeze(cls, bundle, *, user_id, session_id, file_index, attachment, scope, **fields):
        if not isinstance(bundle, DocumentPreparedBundle) or bundle.receipt is None:
            raise RecordDocumentCheckpointError('prepare must return its actual request receipt')
        if bundle.material.scope != scope or scope.user_id != user_id:
            raise RecordDocumentCheckpointError('prepared bundle belongs to another scope')
        return cls.prepared_checkpoint_type(
            user_id=user_id,
            subject_id=scope.subject_id,
            session_id=session_id,
            file_index=file_index,
            document_id=bundle.receipt.document_id,
            source_id=bundle.material.source_id,
            source_revision=bundle.material.source_revision,
            material_snapshot_id=bundle.material.snapshot_id,
            facts_snapshot_id=bundle.facts.snapshot_id,
            understanding_snapshot_id=bundle.understanding.snapshot_id,
            preparation_trace_id=bundle.receipt.trace_id,
            preparation_reused=bundle.receipt.reused,
            attachment_json=json.dumps(dict(attachment), ensure_ascii=False),
            **fields,
        )

    @staticmethod
    def _validate_bundle(bundle, prepared):
        if not isinstance(bundle, DocumentPreparedBundle):
            raise RecordDocumentCheckpointError('a coherent Document bundle is required')
        expected_scope = DocumentScope(prepared.user_id, prepared.subject_id)
        if any(
            (item.scope, item.source_id, item.source_revision)
            != (expected_scope, prepared.source_id, prepared.source_revision)
            for item in (bundle.material, bundle.facts, bundle.understanding)
        ) or (
            bundle.material.snapshot_id,
            bundle.facts.snapshot_id,
            bundle.understanding.snapshot_id,
        ) != (
            prepared.material_snapshot_id,
            prepared.facts_snapshot_id,
            prepared.understanding_snapshot_id,
        ):
            raise RecordDocumentCheckpointError('fixed snapshot references or source identity changed')
        payload = bundle.understanding.payload
        expected_selection = dict(
            zip(
                ('profile_name', 'contract_version', 'implementation_revision'),
                prepared.selection,
                strict=True,
            )
        )
        if (
            payload.get('selection') != expected_selection
            or bundle.material.payload.get('documentId') != prepared.document_id
            or payload.get('documentMetadata', {}).get('documentId') != prepared.document_id
            or payload.get('documentMetadata', {}).get('canonicalReportId') != prepared.source_id
        ):
            raise RecordDocumentCheckpointError('bundle does not match its frozen version or document')
        if not isinstance(payload.get('interpretation'), dict):
            raise RecordDocumentCheckpointError('fixed full interpretation is unavailable')
        return payload['interpretation']

    async def _resolve_scope(self, user_id):
        return await self.ensure_self_scope(user_id), {}

    async def _read(self, prepared):
        bundle = await self.understanding.read_bundle(
            scope=DocumentScope(prepared.user_id, prepared.subject_id),
            understanding_snapshot_id=prepared.understanding_snapshot_id,
            selection=OutputSelection(*prepared.selection),
        )
        return self._validate_bundle(bundle, prepared)

    async def read_projection(
        self,
        *,
        prepared: RecordPreparedDocumentV1,
        publication: RecordDocumentPublication,
    ) -> RecordPreparedDocumentContextV1:
        """Pure fixed reads: no scope creation, processing, or formal publication."""
        interpretation = await self._read(prepared)
        return render_record_document_v1(interpretation, prepared=prepared, publication=publication)

    async def process_attachment(
        self,
        *,
        user_id: str,
        session_id: str,
        file_index: int,
        attachment: Mapping[str, Any],
        settled_document_ids: set[str],
        on_document_ready: Callable[[str], Awaitable[None]] | None = None,
        prepared_checkpoint: Mapping[str, Any] | None = None,
        on_prepared: Callable[[RecordPreparedDocumentV1], Awaitable[None]] | None = None,
    ) -> RecordPreparedDocumentContextV1:
        # Freeze caller-owned mutable input before the first await.
        attachment = json.loads(json.dumps(dict(attachment), ensure_ascii=False))
        if prepared_checkpoint is not None:
            prepared = self.prepared_checkpoint_type.from_checkpoint(prepared_checkpoint)
            prepared.check_request(user_id=user_id, session_id=session_id, file_index=file_index, attachment=attachment)
            interpretation = await self._read(prepared)
        else:
            if not callable(on_prepared):
                raise RecordDocumentCheckpointError('durable on_prepared checkpoint is required before publication')
            scope, fields = await self._resolve_scope(user_id)
            if not isinstance(scope, DocumentScope) or scope.user_id != user_id:
                raise RecordDocumentCheckpointError('self scope resolver returned another user or no explicit scope')
            options = dict(self.preparation_selection)
            if 'lifecycle_epoch' in fields:
                options['lifecycle_epoch'] = fields['lifecycle_epoch']
            if on_document_ready is not None:
                options['on_document_ready'] = on_document_ready
            bundle = await self.understanding.prepare_bundle(
                scope=scope,
                file_id=attachment.get('url'),
                document_id=attachment.get('documentId'),
                file_type=attachment.get('fileType'),
                source_type='record',
                source_id=session_id,
                session_id=session_id,
                file_index=file_index,
                _settled_document_ids=settled_document_ids,
                **options,
            )
            prepared = self._freeze(
                bundle,
                user_id=user_id,
                session_id=session_id,
                file_index=file_index,
                attachment=attachment,
                scope=scope,
                **fields,
            )
            interpretation = self._validate_bundle(bundle, prepared)
            # No publication before this write is acknowledged. Lost publication
            # ACKs replay the exact checkpoint, never a new prepare/latest call.
            await on_prepared(prepared)
        settled_document_ids.add(prepared.document_id)
        publication = await self.publisher.publish_prepared(prepared=prepared)
        if not isinstance(publication, RecordDocumentPublication):
            raise RecordDocumentCheckpointError('publisher returned no supported actual publication receipt')
        return render_record_document_v1(interpretation, prepared=prepared, publication=publication)
