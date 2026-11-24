"""Pure source lifecycle reads; legacy job status cannot prove event visibility."""

from dataclasses import dataclass
from typing import Protocol

from modules.analysis.publication import PublicationReceipt
from modules.analysis.snapshots import DocumentScope


@dataclass(frozen=True, slots=True)
class DocumentSourceLifecycleV1:
    scope: DocumentScope
    source_id: str
    mode: str
    status: str
    publication_revision: int = 0
    accepted_revision: int = 0
    applied_revision: int = 0
    settled_revision: int = 0
    reserved_retraction_revision: int = 0
    accepted_retraction_revision: int = 0
    applied_retraction_revision: int = 0
    active_snapshot_ref: str | None = None
    active_content_digest: str | None = None
    active_material_revision: int | None = None
    active_contract_version: str | None = None
    receipt_state: str | None = None
    application_state: str | None = None
    recovery_state: str | None = None
    error_code: str | None = None
    contract_version: str = 'document.source-lifecycle.v1'

    @property
    def legacy_observations_visible(self):
        # Old ownerRevision is an opaque legacy hash, never a publication counter.
        # A managed source must use its active fixed material, not historical rows.
        return self.mode == 'legacy' and self.status == 'legacy'


class DocumentLifecyclePort(Protocol):
    async def read(self, *, scope: DocumentScope, source_id: str) -> DocumentSourceLifecycleV1: ...


class DocumentPublicationLifecycleReader:
    def __init__(self, *, ledger, snapshots):
        self.ledger = ledger
        self.snapshots = snapshots

    async def read(self, *, scope: DocumentScope, source_id: str) -> DocumentSourceLifecycleV1:
        if not isinstance(scope, DocumentScope) or not isinstance(source_id, str) or not source_id.strip():
            raise ValueError('an explicit scope and canonical source identity are required')
        source = await self.ledger.read_source(scope, source_id)
        if source is None:
            return DocumentSourceLifecycleV1(scope, source_id, 'legacy', 'legacy')
        if source.get('sourceId') != source_id or any(
            source.get(key) != value for key, value in scope.as_dict().items()
        ):
            raise PermissionError('source lifecycle is outside the requested scope')
        revision = source['publicationRevision']
        applied = source['appliedRevision']
        settled = source.get('settledRevision', applied)
        reserved_retraction = source.get('reservedRetractionRevision', 0)
        accepted_retraction = source.get('acceptedRetractionRevision', 0)
        applied_retraction = source.get('appliedRetractionRevision', 0)
        intent = source.get('currentIntent') or {}
        current = intent.get('event') or {}
        saved = await self.ledger.read_intent(scope, intent['_id']) if intent.get('_id') else None
        if saved is not None and saved['event'] != current:
            raise ValueError('the current source intent does not match its immutable outbox')
        receipt = PublicationReceipt(**saved['receipt']) if saved and saved.get('receipt') else None
        if receipt is not None and (
            receipt.source_id != source_id
            or receipt.publication_revision != revision
            or receipt.content_digest != current['contentDigest']
        ):
            raise ValueError('the current source receipt does not match its frozen material')
        confirmed = receipt is not None and receipt.state == 'applied'
        active = None
        if source['mode'] == 'legacy':
            status = 'legacy'
        elif max(reserved_retraction, accepted_retraction) > applied_retraction:
            status = 'retracting'
        elif current.get('eventKind') == 'retract' and settled >= revision and confirmed:
            status = 'withdrawn'
        elif revision and applied == revision and confirmed and current.get('eventKind') != 'retract':
            status = 'active'
            active = current
        else:
            status = 'pending'
        return DocumentSourceLifecycleV1(
            scope=scope,
            source_id=source_id,
            mode=source['mode'],
            status=status,
            publication_revision=revision,
            accepted_revision=source.get('acceptedRevision', applied),
            applied_revision=applied,
            settled_revision=settled,
            reserved_retraction_revision=reserved_retraction,
            accepted_retraction_revision=accepted_retraction,
            applied_retraction_revision=applied_retraction,
            active_snapshot_ref=active['snapshotRef']['snapshotId'] if active else None,
            active_content_digest=active['contentDigest'] if active else None,
            active_material_revision=active['sourceRevision'] if active else None,
            active_contract_version=active['snapshotRef']['contractVersion'] if active else None,
            receipt_state=receipt.state if receipt else None,
            application_state=receipt.application_state if receipt else None,
            recovery_state=saved.get('recoveryState') if saved else None,
            error_code=(saved.get('recoveryError') or (receipt.error_code if receipt else None)) if saved else None,
        )

    async def read_active_material(self, *, scope: DocumentScope, source_id: str, expected=None):
        """Return only a currently active fixed snapshot, with a bounded race check."""
        before = await self.read(scope=scope, source_id=source_id)
        if expected is not None and before != expected:
            raise RuntimeError('source lifecycle no longer matches the selected material')
        if before.status != 'active':
            return None
        snapshot = await self.snapshots.get(scope, before.active_snapshot_ref)
        if (
            snapshot.source_id != source_id
            or snapshot.source_revision != before.active_material_revision
            or snapshot.contract_version != before.active_contract_version
            or snapshot.content_digest != before.active_content_digest
        ):
            raise ValueError('active publication material does not match its fixed lifecycle binding')
        if await self.read(scope=scope, source_id=source_id) != before:
            raise RuntimeError('source lifecycle changed while reading active material')
        return snapshot

    async def read_sources(self, *, scope, after_id=None, limit=100):
        if not isinstance(scope, DocumentScope) or type(limit) is not int or not 1 <= limit <= 1001:
            raise ValueError('an explicit scope and bounded source scan are required')
        return await self.ledger.read_sources(scope, after_id=after_id, limit=limit)
