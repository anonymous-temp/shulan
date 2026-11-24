"""Current complete-source publication on the existing durable ledger and recovery."""

from modules.analysis.publication import DocumentPublisher
from modules.analysis.publication import PublicationConflictError
from modules.analysis.snapshots import COMPLETE_SOURCE_CONTRACT_V2
from modules.analysis.snapshots import RETRACTION_CONTRACT
from modules.analysis.snapshots import DocumentSnapshot
from modules.analysis.snapshots import canonical_json
from modules.analysis.snapshots import digest


class DocumentCurrentSourcePublisher(DocumentPublisher):
    def __init__(self, *, source_reader, snapshots, ledger, recovery):
        if recovery is None or recovery.ledger is not ledger:
            raise ValueError('publication requires the shared durable recovery ledger')
        super().__init__(snapshots=snapshots, ledger=ledger, receiver=recovery.receiver, recovery=recovery)
        self.source_reader = source_reader

    async def publish(self, *, scope, snapshot_id, request_id, expected_prior_revision, event_kind='publish'):
        if event_kind not in {'publish', 'correct'}:
            raise ValueError('use retract for source withdrawal')
        source, proof = await self.source_reader.read_publication_material(scope=scope, snapshot_id=snapshot_id)
        if (
            source['scope'] != scope.as_dict()
            or source['contractVersion'] != COMPLETE_SOURCE_CONTRACT_V2
            or source['sourceType'] != 'health_report'
            or source['payload']['quality']['status'] != 'accepted'
            or snapshot_id != 'document_source_snapshot_' + digest(source)
        ):
            raise ValueError('the current accepted source capture is required')
        event = {
            'scope': source['scope'],
            'sourceType': source['sourceType'],
            'sourceId': source['sourceId'],
            'sourceRevision': source['sourceRevision'],
            'eventKind': event_kind,
            'contentDigest': digest(source),
            'snapshotRef': {
                'provider': 'document',
                'contractVersion': COMPLETE_SOURCE_CONTRACT_V2,
                'snapshotId': snapshot_id,
            },
        }
        return await self._deliver(scope, event, request_id, expected_prior_revision, current_proof=proof)

    async def retract(self, *, scope, source_id, source_revision, request_id, expected_prior_revision, lifecycle_epoch):
        if type(lifecycle_epoch) is not int or lifecycle_epoch < 1:
            raise ValueError('the original fixed subject lifecycle is required')
        source = await self.ledger.read_source(scope, source_id)
        binding = (source or {}).get('currentSourceBinding', {})
        if binding.get('lifecycleEpoch') != lifecycle_epoch or binding.get('sourceRevision') != source_revision:
            raise PublicationConflictError('retraction differs from the original source lifecycle')
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
                    'lifecycleEpoch': lifecycle_epoch,
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
        }
        return await self._deliver(
            scope, event, request_id, expected_prior_revision, current_proof={'lifecycleEpoch': lifecycle_epoch}
        )
