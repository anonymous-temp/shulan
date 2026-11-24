"""Adapt Profile's public legacy status without reading its private job store."""

from modules.analysis.observation_reader import DocumentObservationReadError
from modules.analysis.snapshots import DocumentScope
from modules.health_profile.contracts import ProfileLegacyIngestionStatusV1
from modules.health_profile.contracts import ProfileScope


class ProfileDocumentObservationIngestionReader:
    def __init__(self, reader):
        self.reader = reader

    async def read_status(self, *, scope: DocumentScope, source_id: str, source_revision: int) -> str:
        if not isinstance(scope, DocumentScope):
            raise TypeError('an explicit DocumentScope is required')
        profile_scope = ProfileScope(user_id=scope.user_id, subject_id=scope.subject_id)
        result = await self.reader.read_health_report(
            profile_scope, canonical_report_id=source_id, legacy_source_revision=source_revision
        )
        if (
            not isinstance(result, ProfileLegacyIngestionStatusV1)
            or result.contract_version != 'profile-legacy-ingestion-status.v1'
            or result.scope != profile_scope
            or result.canonical_report_id != source_id
            or result.legacy_source_revision != source_revision
        ):
            raise DocumentObservationReadError('Profile returned an incompatible ingestion status binding')
        if result.status != 'ready' or result.ingestion_status is None:
            # An unavailable or foreign source must not masquerade as pending.
            # Domain memory treats this supplement as optional, separately from
            # its authoritative Profile fact page.
            raise DocumentObservationReadError('Profile ingestion status is unavailable')
        return result.ingestion_status
