"""Bounded, explicitly scheduled source outbox recovery; never run by a GET."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Protocol

from modules.analysis.publication import PublicationReceipt
from modules.analysis.snapshots import COMPLETE_SOURCE_CONTRACT
from modules.analysis.snapshots import COMPLETE_SOURCE_CONTRACT_V2
from modules.analysis.snapshots import RETRACTION_CONTRACT
from modules.analysis.snapshots import DocumentScope

RECOVERY_MATERIAL_CONTRACTS = {
    'default': COMPLETE_SOURCE_CONTRACT,
    'current': COMPLETE_SOURCE_CONTRACT_V2,
}


class PublicationRecoveryError(RuntimeError):
    pass


class PublicationLeaseLost(PublicationRecoveryError):
    pass


class PublicationReceiptReader(Protocol):
    async def lookup(
        self, event: dict, *, known_receipt: PublicationReceipt | None = None
    ) -> PublicationReceipt | None: ...

    async def deliver(self, event: dict) -> PublicationReceipt: ...


@dataclass(frozen=True, slots=True)
class PublicationRecoveryPolicy:
    max_dispatches: int = 5
    max_transport_failures: int = 5
    deadline_seconds: int = 86400
    lease_seconds: int = 60
    io_timeout_seconds: int = 20
    poll_seconds: int = 30

    def __post_init__(self):
        if any(type(value) is not int or value < 1 for value in vars_without_slots(self)):
            raise ValueError('recovery budgets must be positive integers')
        if self.io_timeout_seconds * 2 >= self.lease_seconds:
            raise ValueError('each bounded receiver call must fit inside half the lease')


def vars_without_slots(value):
    return (getattr(value, field) for field in value.__dataclass_fields__)


@dataclass(frozen=True, slots=True)
class PublicationRecoveryBatch:
    repaired: int
    processed: int
    deferred: int
    truncated: bool
    next_source_id: str | None
    next_intent_id: str | None


class DocumentPublicationRecovery:
    def __init__(self, *, ledger, receiver: PublicationReceiptReader, policy=None, clock=None, recovery_route=None):
        if recovery_route is not None and recovery_route not in RECOVERY_MATERIAL_CONTRACTS:
            raise ValueError('unknown Document recovery route')
        self.recovery_route = recovery_route
        self.ledger = ledger
        self.receiver = receiver
        self.policy = policy or PublicationRecoveryPolicy()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def now_ms(self):
        value = self.clock()
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValueError('publication recovery requires an aware clock')
        return int(value.timestamp() * 1000)

    def timeout_seconds(self, claimed):
        remaining = (claimed['recoveryDeadlineMs'] - self.now_ms()) / 1000
        if remaining <= 0:
            raise PublicationRecoveryError('document.publication_deadline_exceeded')
        policy = PublicationRecoveryPolicy(**claimed['recoveryPolicy'])
        return min(policy.io_timeout_seconds, remaining)

    async def _owns_intent(self, scope, intent):
        route = self.recovery_route
        if route is None:
            return True
        event = intent['event']
        ref = event['snapshotRef']
        if event['sourceType'] != 'health_report' or ref['provider'] != 'document':
            return False
        frozen_route = intent.get('recoveryRoute')
        if frozen_route is not None and frozen_route != route:
            return False
        contract = ref['contractVersion']
        if contract != RETRACTION_CONTRACT:
            return contract == RECOVERY_MATERIAL_CONTRACTS[route]
        if event['eventKind'] != 'retract':
            return False
        if frozen_route is not None:
            return True
        # Existing shared retractions carry the same material contract for both routes.
        source = await self.ledger.read_source(scope, event['sourceId'])
        if source is None:
            return False
        return (source.get('currentSourceBinding') is not None) == (route == 'current')

    async def deliver_intent(self, *, scope: DocumentScope, intent_id: str):
        prior = await self.ledger.read_intent(scope, intent_id)
        if prior is not None and not await self._owns_intent(scope, prior):
            raise PublicationRecoveryError('publication belongs to another recovery route')
        saved = prior.get('receipt') if prior else None
        if saved is not None and saved['state'] in {'applied', 'superseded'}:
            receipt = PublicationReceipt(**saved)
            await self.ledger.acknowledge(scope, prior, receipt)
            await self.ledger.reconcile_terminal(prior, now=self.now_ms())
            return receipt
        claimed = await self.ledger.claim_intent(scope, intent_id, now=self.now_ms(), policy=self.policy)
        if claimed is None:
            raise PublicationRecoveryError('publication is leased, terminal, or awaiting explicit redrive')
        event = claimed['event']
        policy = PublicationRecoveryPolicy(**claimed['recoveryPolicy'])
        receipt = None
        try:
            # Reconcile acceptance first: a lost ACK must not blindly redeliver.
            async with asyncio.timeout(self.timeout_seconds(claimed)):
                receipt = await self.receiver.lookup(
                    event, known_receipt=PublicationReceipt(**claimed['receipt']) if claimed.get('receipt') else None
                )
            if receipt is None:
                claimed = await self.ledger.before_dispatch(claimed, now=self.now_ms(), policy=policy)
                async with asyncio.timeout(self.timeout_seconds(claimed)):
                    receipt = await self.receiver.deliver(event)
            await self.ledger.acknowledge(scope, claimed, receipt, lease=claimed, now=self.now_ms())
            terminal = receipt.state in {'applied', 'superseded', 'failed', 'partial'}
            await self.ledger.finish_attempt(
                claimed,
                now=self.now_ms(),
                state=receipt.state if terminal else 'polling',
                delay_ms=policy.poll_seconds * 1000,
                error_code=receipt.error_code,
            )
            return receipt
        except PublicationLeaseLost:
            raise
        except asyncio.CancelledError:
            # Dispatch reservations survive cancellation; expiry enables recovery.
            raise
        except Exception as exc:
            # A per-intent failure cannot poison the rest of a bounded scan.
            await self.ledger.fail_attempt(claimed, now=self.now_ms(), policy=policy, error_code=type(exc).__name__)
            raise

    async def recover_pending(self, *, limit=100, after_source_id=None, after_intent_id=None):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('recovery scan limit must be between 1 and 1000')
        route = {'recovery_route': self.recovery_route} if self.recovery_route is not None else {}
        repaired, repair_more, source_cursor = await self.ledger.repair_outbox(
            limit=limit, after_id=after_source_id, **route
        )
        rows, more = await self.ledger.scan_pending(now=self.now_ms(), limit=limit, after_id=after_intent_id, **route)
        processed = deferred = 0
        for row in rows:
            scope = DocumentScope(row['userId'], row['subjectId'])
            try:
                if not await self._owns_intent(scope, row):
                    continue
                await self.deliver_intent(scope=scope, intent_id=row['_id'])
                processed += 1
            except Exception:
                deferred += 1
        return PublicationRecoveryBatch(
            repaired,
            processed,
            deferred,
            repair_more or more,
            source_cursor,
            rows[-1]['_id'] if more and rows else None,
        )

    async def redrive(self, *, scope, intent_id, reason):
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('explicit redrive requires an audit reason')
        if self.recovery_route is not None:
            prior = await self.ledger.read_intent(scope, intent_id)
            if prior is not None and not await self._owns_intent(scope, prior):
                raise PublicationRecoveryError('publication belongs to another recovery route')
        await self.ledger.redrive_intent(scope, intent_id, now=self.now_ms(), policy=self.policy, reason=reason)
