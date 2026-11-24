"""Explicit Document source-wide publication port; receipt acceptance is not application.

The Profile adapter is deliberately injected. No inbox DTO or private Profile
schema is copied here, and this use case does not activate the production switch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from modules.analysis.snapshots import FACTS_CONTRACT
from modules.analysis.snapshots import RETRACTION_CONTRACT
from modules.analysis.snapshots import DocumentScope
from modules.analysis.snapshots import DocumentSnapshot
from modules.analysis.snapshots import canonical_json


class PublicationConflictError(ValueError):
    pass


class PublicationModeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PublicationReceipt:
    source_id: str
    publication_revision: int
    content_digest: str
    state: str
    receipt_ref: str
    application_state: str | None = None
    accepted_revision: int | None = None
    applied_revision: int | None = None
    settled_revision: int | None = None
    accepted_retraction_revision: int = 0
    applied_retraction_revision: int = 0
    error_code: str | None = None
    superseded_by_revision: int | None = None

    def __post_init__(self):
        if self.state not in {
            'pending',
            'received',
            'waiting',
            'processing',
            'retry_wait',
            'partial',
            'applied',
            'failed',
            'superseded',
        }:
            raise ValueError('unsupported publication receipt state')
        if not self.receipt_ref:
            raise ValueError('a durable receiver receipt is required')
        if self.application_state not in {
            None,
            'not_applied',
            'incomplete',
            'partial',
            'applied',
            'superseded_by_retraction',
        }:
            raise ValueError('unsupported publication application state')
        for name in (
            'publication_revision',
            'accepted_revision',
            'applied_revision',
            'settled_revision',
            'accepted_retraction_revision',
            'applied_retraction_revision',
            'superseded_by_revision',
        ):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < (1 if name == 'publication_revision' else 0)):
                raise ValueError('publication watermarks must be nonnegative exact integers')
        if self.application_state is not None and (self.state == 'applied') != (self.application_state == 'applied'):
            raise ValueError('applied receipt and application state must agree')
        if (
            self.state == 'applied'
            and self.applied_revision is not None
            and self.applied_revision < self.publication_revision
        ):
            raise ValueError('applied receipt requires an application watermark covering this event')
        if self.state == 'partial' and (
            self.application_state != 'partial'
            or self.settled_revision is not None
            and self.settled_revision >= self.publication_revision
        ):
            raise ValueError('partial is incomplete source application, not settlement')
        if self.state == 'superseded' and (
            self.application_state != 'superseded_by_retraction'
            or self.superseded_by_revision is None
            or self.superseded_by_revision <= self.publication_revision
            or self.settled_revision is None
            or self.settled_revision < self.superseded_by_revision
            or self.applied_retraction_revision < self.superseded_by_revision
        ):
            raise ValueError('superseded requires the later retraction settlement proof')


@dataclass(frozen=True, slots=True)
class PublicationCutoverEvidence:
    expected_publication_revision: int
    receiver_applied_revision: int
    receiver_accepted_retraction_revision: int
    receiver_receipt_ref: str
    legacy_quiescence_receipt: str

    def validate(self, revision):
        # These references are supplied by the trusted migration coordinator
        # after receiver reconciliation and stopping/draining old producers.
        if not self.receiver_receipt_ref or not self.legacy_quiescence_receipt:
            raise PublicationModeError('cutover requires receiver and legacy quiescence evidence')
        if (
            self.expected_publication_revision != revision
            or self.receiver_applied_revision != revision
            or self.receiver_accepted_retraction_revision > self.receiver_applied_revision
        ):
            raise PublicationModeError('publication watermarks are not reconciled')


class DocumentPublicationPort(Protocol):
    async def deliver(self, event: dict) -> PublicationReceipt: ...


class DocumentPublisher:
    """Retry sends the same frozen event. A new revision requires prior application.

    Corrections replace the whole source, retractions remove its contribution.
    Per-observation supersedes is unsupported when physical identity is missing.
    """

    def __init__(self, *, snapshots, ledger, receiver: DocumentPublicationPort, recovery=None):
        self.snapshots = snapshots
        self.ledger = ledger
        self.receiver = receiver
        self.recovery = recovery

    async def publish(
        self,
        *,
        scope: DocumentScope,
        snapshot_id: str,
        request_id: str,
        expected_prior_revision: int | None,
        event_kind='publish',
    ) -> PublicationReceipt:
        if event_kind not in {'publish', 'correct'}:
            raise ValueError('use retract for source withdrawal')
        snapshot = await self.snapshots.get(scope, snapshot_id)
        if snapshot.contract_version != FACTS_CONTRACT:
            raise ValueError('publication requires a fixed facts snapshot')
        if snapshot.payload.get('quality', {}).get('status') == 'unavailable':
            raise ValueError('unavailable interpretation cannot publish clinical facts')
        event = {
            'scope': scope.as_dict(),
            'sourceType': 'health_report',
            'sourceId': snapshot.source_id,
            'sourceRevision': snapshot.source_revision,
            'eventKind': event_kind,
            'contentDigest': snapshot.content_digest,
            'snapshotRef': {'provider': 'document', 'contractVersion': FACTS_CONTRACT, 'snapshotId': snapshot_id},
            'legacySourceRevision': snapshot.payload.get('legacySourceRevision'),
        }
        return await self._deliver(scope, event, request_id, expected_prior_revision)

    async def retract(
        self,
        *,
        scope: DocumentScope,
        source_id: str,
        source_revision: int,
        request_id: str,
        expected_prior_revision: int,
    ) -> PublicationReceipt:
        snapshot = DocumentSnapshot(
            scope,
            RETRACTION_CONTRACT,
            source_id,
            source_revision,
            canonical_json(
                {
                    'publicationRevision': expected_prior_revision + 1,
                    'priorRevision': expected_prior_revision,
                    'eventKind': 'retract',
                }
            ),
        )
        await self.snapshots.put(snapshot)
        event = {
            'scope': scope.as_dict(),
            'sourceType': 'health_report',
            'sourceId': source_id,
            'sourceRevision': source_revision,
            'eventKind': 'retract',
            'contentDigest': snapshot.content_digest,
            'snapshotRef': {
                'provider': 'document',
                'contractVersion': RETRACTION_CONTRACT,
                'snapshotId': snapshot.snapshot_id,
            },
            'legacySourceRevision': None,
        }
        return await self._deliver(scope, event, request_id, expected_prior_revision)

    async def _deliver(self, scope, event, request_id, expected_prior, *, current_proof=None):
        # Canonical roundtrip detaches every caller-owned object before awaiting.
        import json

        event = json.loads(canonical_json(event))
        reservation = {} if current_proof is None else {'current_proof': current_proof}
        intent = await self.ledger.reserve(scope, event, request_id, expected_prior, **reservation)
        if self.recovery is not None:
            return await self.recovery.deliver_intent(scope=scope, intent_id=intent['_id'])
        receipt = intent.get('receipt')
        if receipt is not None and receipt['state'] == 'applied':
            restored = PublicationReceipt(**receipt)
            await self.ledger.acknowledge(scope, intent, restored)
            return restored
        received = await self.receiver.deliver(json.loads(canonical_json(intent['event'])))
        await self.ledger.acknowledge(scope, intent, received)
        return received
