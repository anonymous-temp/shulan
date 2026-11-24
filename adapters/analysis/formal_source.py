"""Public Profile bridges for Document's own formal publication workflow."""

from datetime import UTC
from datetime import datetime

from modules.analysis.formal_source_contracts import DocumentFormalSourceConflict
from modules.analysis.snapshots import COMPLETE_SOURCE_CONTRACT
from modules.health_profile.contracts import ProfileFreshSourceAuthorizationV1
from modules.health_profile.contracts import ProfileScope


class ProfileDocumentFormalAdmission:
    def __init__(self, admission):
        self.admission = admission

    @staticmethod
    def supports_scope(scope):
        try:
            ProfileScope(user_id=scope.user_id, subject_id=scope.subject_id)
        except ValueError:
            return False
        return True

    def authorization(self, *, command_id, scope, source_id, captured, fence, accepted_at):
        return ProfileFreshSourceAuthorizationV1(
            scope=ProfileScope(user_id=scope.user_id, subject_id=scope.subject_id),
            source_type='health_report',
            source_id=source_id,
            authorization_id=command_id,
            owner_fence_ref=fence['ownerFenceRef'],
            owner_fence_digest=fence['ownerFenceDigest'],
            presence_proof_ref=fence['profilePresenceProofRef'],
            presence_proof_digest=fence['profilePresenceProofDigest'],
            material_contract_version=COMPLETE_SOURCE_CONTRACT,
            material_snapshot_ref={
                'provider': 'document',
                'contractVersion': COMPLETE_SOURCE_CONTRACT,
                'snapshotId': captured['source_snapshot_ref'],
            },
            material_content_digest=captured['source_content_digest'],
            processing_revision='profile-document-source-v1',
            authorized_at=datetime.fromtimestamp(accepted_at, UTC),
        ).model_dump(mode='json', by_alias=True)

    async def reserve(self, authorization):
        return await self._apply(authorization, self.admission.reserve_fresh_source, {'blocked', 'ready'})

    async def authorize(self, authorization):
        return await self._apply(authorization, self.admission.authorize_fresh_source, {'ready'})

    @staticmethod
    async def _apply(raw, method, states):
        authorization = ProfileFreshSourceAuthorizationV1.model_validate(raw)
        value = await method(authorization, scope=authorization.scope)
        if value.authorization != authorization or value.status not in states:
            raise DocumentFormalSourceConflict('Profile admission acknowledged different fixed Document material')
        return value.model_dump(mode='json', by_alias=True)


def create_document_formal_source_service(
    *,
    compatibility,
    source_admission,
    managed,
    report_publications,
    profile_admission,
    command_records,
    policy=None,
    clock=None,
):
    """Construct without I/O; all providers must use the same fixed Document gate.

    The root registers recover_pending separately and owns clients/collection
    provisioning. This constructor neither starts a worker nor selects a default.
    """
    from adapters.analysis.formal_source_store import MongoDocumentFormalSourceStore
    from modules.analysis.formal_legacy_report import DocumentFormalLegacyReport
    from modules.analysis.formal_source import DocumentFormalSourceService
    from modules.analysis.formal_source_steps import DocumentFormalSourceSteps

    if (
        report_publications.compatibility is not compatibility
        or source_admission.compatibility is not compatibility
        or managed.commands.initial_admission is not source_admission
        or compatibility.legacy_publication.ledger is not managed.publisher.ledger
    ):
        raise ValueError('formal Document commands must share the actual providers and publication gate')
    steps = DocumentFormalSourceSteps(
        compatibility=compatibility,
        admission=source_admission,
        managed=managed,
        profile_admission=ProfileDocumentFormalAdmission(profile_admission),
        legacy_reports=DocumentFormalLegacyReport(publications=report_publications, managed=managed),
    )
    options = {}
    if policy is not None:
        options['policy'] = policy
    if clock is not None:
        options['clock'] = clock
    return DocumentFormalSourceService(store=MongoDocumentFormalSourceStore(command_records), steps=steps, **options)
