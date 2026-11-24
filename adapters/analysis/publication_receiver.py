"""Document transport using only Profile's public source Inbox and DTOs."""

from health_profile.contracts import ProfileScope
from health_profile.contracts import ProfileSourceEventV1
from health_profile.contracts import ProfileSourceReceiptV1
from modules.analysis.publication import PublicationConflictError
from modules.analysis.publication import PublicationModeError
from modules.analysis.publication import PublicationReceipt


class DocumentProfileReceiptTransport:
    """Common transport validation; no source authorization or publication side effects on lookup."""

    def __init__(self, inbox, *, material_contract_version='document.source-observations.v1'):
        self.inbox = inbox
        self.material_contract_version = material_contract_version

    def _event(self, event):
        value = ProfileSourceEventV1.model_validate(event)
        contract = 'document.retraction.v1' if value.event_kind == 'retract' else self.material_contract_version
        if (
            value.source_type != 'health_report'
            or value.snapshot_ref.provider != 'document'
            or value.snapshot_ref.contract_version != contract
        ):
            raise PublicationModeError('the exact Document material capability is unavailable')
        return value

    async def lookup(self, event, *, known_receipt=None):
        value = self._event(event)
        receipt = await self.inbox.get_receipt(value.event_id, scope=value.scope)
        if receipt is None:
            return None
        result = self._receipt(value, receipt)
        # The current public Profile receipt does not expose the frozen content
        # digest. Only a locally persisted, validated receive ACK proves the
        # event binding. Without it, resend the exact event through receive's
        # fingerprint check instead of asserting acceptance from ID alone.
        if known_receipt is None:
            return None
        if (
            known_receipt.source_id != value.source_id
            or known_receipt.publication_revision != value.publication_revision
            or known_receipt.content_digest != value.content_digest
            or known_receipt.receipt_ref != receipt.publication_id
        ):
            raise PublicationConflictError('the prior acceptance proof does not match the frozen source event')
        return result

    @staticmethod
    def _receipt(event, receipt):
        if not isinstance(receipt, ProfileSourceReceiptV1):
            raise TypeError('the public Profile receipt contract is required')
        receipt = ProfileSourceReceiptV1.model_validate(receipt.model_dump())
        watermark = receipt.watermark
        if (
            receipt.scope != event.scope
            or receipt.source_type != event.source_type
            or receipt.source_id != event.source_id
            or receipt.event_id != event.event_id
            or receipt.publication_revision != event.publication_revision
            or watermark.scope != event.scope
            or watermark.source_type != event.source_type
            or watermark.source_id != event.source_id
            or not receipt.publication_id
        ):
            raise PublicationConflictError('Profile receipt does not identify the exact frozen source event')
        if watermark.accepted_publication_revision < event.publication_revision:
            # A staged event alias is not acceptance. Redeliver the same event to
            # repair a crash before the Profile source cursor CAS.
            return None
        return PublicationReceipt(
            source_id=event.source_id,
            publication_revision=event.publication_revision,
            content_digest=event.content_digest,
            state=receipt.status,
            receipt_ref=receipt.publication_id,
            application_state=receipt.application_state,
            accepted_revision=watermark.accepted_publication_revision,
            applied_revision=watermark.applied_publication_revision,
            settled_revision=watermark.settled_publication_revision,
            accepted_retraction_revision=watermark.accepted_retraction_revision,
            applied_retraction_revision=watermark.applied_retraction_revision,
            error_code=receipt.error_code,
            superseded_by_revision=receipt.superseded_by_publication_revision,
        )

    async def _receive(self, value):
        receipt = await self.inbox.receive(value, scope=value.scope)
        result = self._receipt(value, receipt)
        if result is None:
            raise PublicationConflictError('receiver acceptance has not reached its durable source watermark')
        return result


class ProfileCurrentDocumentPublicationReceiver(DocumentProfileReceiptTransport):
    """The current Profile inbox validates fixed evidence and binds its original epoch."""

    def __init__(self, inbox):
        super().__init__(inbox, material_contract_version='document.source-observations.v2')

    async def deliver(self, event):
        return await self._receive(self._event(event))


class ProfileDocumentPublicationReceiver(DocumentProfileReceiptTransport):
    """Temporary existing default transport, removed with its default composition."""

    async def deliver(self, event):
        value = self._event(event)
        ready = await self.inbox.get_cutover(value.scope, value.source_type, value.source_id)
        if (
            ready is None
            or ready.status != 'ready'
            or ready.authorization.scope != value.scope
            or ready.authorization.source_type != value.source_type
            or ready.authorization.source_id != value.source_id
            or ready.authorization.material_contract_version != self.material_contract_version
        ):
            raise PublicationModeError('Profile has not authorized this source and exact complete material contract')
        return await self._receive(value)

    async def ready(self, *, scope, source_id):
        """Pure readiness read, never authorizes quiescence on a caller's behalf."""
        return await self.inbox.get_cutover(ProfileScope(**scope.as_dict()), 'health_report', source_id)
