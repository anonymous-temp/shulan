"""Explicit complete-source publication; the older Facts publisher stays separate."""

from modules.analysis.publication import DocumentPublisher
from modules.analysis.snapshots import COMPLETE_SOURCE_CONTRACT
from modules.analysis.snapshots import digest


class DocumentCompleteSourcePublisher(DocumentPublisher):
    VERSION = 'document-complete-source-publication-v1'
    SOURCE_CONTRACT = COMPLETE_SOURCE_CONTRACT

    def __init__(self, *, source_reader, snapshots, ledger, recovery):
        if recovery is None or recovery.ledger is not ledger:
            raise ValueError('complete-source publication requires the shared durable recovery ledger')
        super().__init__(snapshots=snapshots, ledger=ledger, receiver=recovery.receiver, recovery=recovery)
        self.source_reader = source_reader

    async def publish(self, *, scope, snapshot_id, request_id, expected_prior_revision, event_kind='publish'):
        if event_kind not in {'publish', 'correct'}:
            raise ValueError('use retract for source withdrawal')
        # This public read verifies the complete capture proof, inventory and all
        # original inputs. Loading a raw snapshot alone cannot prove acceptance.
        source = await self.source_reader.read_source_observations(scope=scope, snapshot_id=snapshot_id)
        if (
            source['scope'] != scope.as_dict()
            or source['contractVersion'] != self.SOURCE_CONTRACT
            or source['sourceType'] != 'health_report'
            or source['payload']['quality']['status'] != 'accepted'
            or snapshot_id != 'document_source_snapshot_' + digest(source)
        ):
            raise ValueError('the exact accepted complete-source capture is required')
        event = {
            'scope': source['scope'],
            'sourceType': source['sourceType'],
            'sourceId': source['sourceId'],
            'sourceRevision': source['sourceRevision'],
            'eventKind': event_kind,
            'contentDigest': digest(source),
            'snapshotRef': {
                'provider': 'document',
                'contractVersion': self.SOURCE_CONTRACT,
                'snapshotId': snapshot_id,
            },
            'legacySourceRevision': source['payload']['coverage']['legacySourceRevision'],
        }
        return await self._deliver(scope, event, request_id, expected_prior_revision)
