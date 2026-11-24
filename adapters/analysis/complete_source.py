"""Zero-IO complete-source assembly and public Profile scope translation."""

from modules.analysis.complete_source import DocumentCompleteSourceService
from modules.analysis.complete_source_publication import DocumentCompleteSourcePublisher
from modules.analysis.snapshots import DocumentScope
from modules.health_profile.contracts import ProfileScope


class ProfileDocumentCompleteSourceReader:
    def __init__(self, service):
        self.service = service

    @staticmethod
    def _scope(scope):
        if not isinstance(scope, ProfileScope):
            raise TypeError('an explicit Profile scope is required')
        return DocumentScope(scope.user_id, scope.subject_id)

    async def read_source_observations(self, *, scope, snapshot_id):
        return await self.service.read_source_observations(scope=self._scope(scope), snapshot_id=snapshot_id)

    async def read_source_inventory(self, *, scope, snapshot_id):
        return await self.service.read_source_inventory(scope=self._scope(scope), snapshot_id=snapshot_id)

    async def read_retraction(self, *, scope, snapshot_id):
        return await self.service.read_retraction(scope=self._scope(scope), snapshot_id=snapshot_id)


def create_document_complete_source_service(*, snapshots, understanding, report_publications, admission):
    """Inject existing fixed readers and a verified durable admission provider.

    Collections remain owned by the existing snapshot and source-ledger factories:
    PRIMARY reads, majority writes, unique _id, no TTL. This factory creates no
    clients, indexes, workers, admission proof or source-mode changes. An absent
    admission provider leaves standalone contribution capture available but cannot
    manufacture a complete source. Production defaults are intentionally unchanged.
    """
    return DocumentCompleteSourceService(
        snapshots=snapshots, understanding=understanding, report_publications=report_publications, admission=admission
    )


def create_document_complete_source_publisher(*, source_reader, snapshots, publication_runtime):
    """Share the exact runtime ledger used by every legacy writer and the outbox."""
    return DocumentCompleteSourcePublisher(
        source_reader=source_reader,
        snapshots=snapshots,
        ledger=publication_runtime.ledger,
        recovery=publication_runtime.recovery,
    )
