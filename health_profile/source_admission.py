"""Two-phase fresh admission: owner fence plus independently observed absence."""

from __future__ import annotations

from copy import deepcopy

from .contracts import ProfileScope
from .contracts import profile_content_digest
from .source_admission_contracts import ProfileFreshSourceAuthorizationV1
from .source_admission_contracts import ProfileFreshSourceStateV1
from .source_admission_contracts import ProfileOwnerAdmissionFenceV1
from .source_publications import ProfileSourceConflictError
from .source_publications import source_key


class ProfileFreshSourceAdmission:
    """A reservation fences legacy delivery but never grants event publication.

    Source owners may commit business results with durable new-mode intents
    before background admission; event publication still requires authorization.
    Failure retains the blocked marker without undoing business completion, and
    this API never silently falls back to legacy.
    """

    def __init__(self, *, inbox, presence_reader, owner_fence_readers):
        self.inbox = inbox
        self.presence = presence_reader
        self.owners = dict(owner_fence_readers)

    def _authorization(self, authorization, scope):
        authorization = ProfileFreshSourceAuthorizationV1.model_validate(authorization.model_dump(mode='python'))
        if not isinstance(scope, ProfileScope) or authorization.scope != scope:
            raise PermissionError('fresh source authorization differs from resolved scope')
        self.inbox._require_cutover_capability(authorization.source_type, authorization.material_contract_version)
        if authorization.source_type == 'health_report':
            from .document_source import validate_default_document_declaration
            from .source_material import ProfileSourceApplicationError

            try:
                validate_default_document_declaration(authorization)
            except ProfileSourceApplicationError as error:
                raise ProfileSourceConflictError('source.document_complete_source_required') from error
        elif (
            authorization.material_contract_version
            not in {
                'record': {'profile.record-source.v2'},
                'chat': {'profile.chat-source.v2'},
                'questionnaire': {'profile.questionnaire-source.v1', 'profile.questionnaire-source.v2'},
            }[authorization.source_type]
        ):
            raise ProfileSourceConflictError('source.fresh_processing_contract_unsupported')
        return authorization

    async def _owner(self, authorization):
        ref = authorization.owner_fence_ref
        expected_provider = 'document' if authorization.source_type == 'health_report' else authorization.source_type
        if ref.provider != expected_provider:
            raise ProfileSourceConflictError('source.owner_fence_provider_mismatch')
        if ref.contract_version != 'profile-owner-admission-fence.v1':
            raise ProfileSourceConflictError('source.owner_fence_contract_unsupported')
        reader = self.owners.get(ref.provider)
        if reader is None:
            raise ProfileSourceConflictError('source.owner_fence_reader_unavailable')
        raw = deepcopy(await reader.read_admission_fence(scope=authorization.scope, snapshot_ref=ref))
        proof = ProfileOwnerAdmissionFenceV1.model_validate(raw)
        if (
            proof.scope != authorization.scope
            or proof.source_type != authorization.source_type
            or proof.source_id != authorization.source_id
            or profile_content_digest(raw) != authorization.owner_fence_digest
        ):
            raise ProfileSourceConflictError('source.owner_fence_binding_mismatch')
        if (
            await reader.is_admission_fence_held(
                scope=authorization.scope,
                snapshot_ref=ref,
                content_digest=authorization.owner_fence_digest,
            )
            is not True
        ):
            raise ProfileSourceConflictError('source.owner_fence_not_held')
        return raw

    async def _absence(self, authorization, marker_id=None):
        proof = await self.presence.read_legacy_presence(
            authorization.scope,
            source_type=authorization.source_type,
            source_id=authorization.source_id,
            **({'_ignore_marker': marker_id} if marker_id else {}),
        )
        expected = {'jobs', 'fact_contributions', 'publications'}
        if authorization.source_type == 'health_report':
            expected |= {'reports', 'report_revisions'}
        if (
            proof.scope != authorization.scope
            or proof.source_type != authorization.source_type
            or proof.source_id != authorization.source_id
            or proof.status != 'absent'
            or {section.kind for section in proof.sections} != expected
            or any(section.observed_count or section.truncated for section in proof.sections)
            or proof.proof_ref != authorization.presence_proof_ref
            or proof.content_digest != authorization.presence_proof_digest
        ):
            raise ProfileSourceConflictError('source.fresh_absence_not_proven')
        return proof

    @staticmethod
    def _same(row, authorization):
        if row.get('admissionKind') != 'fresh' or row.get('authorization') != authorization.model_dump(
            mode='json', by_alias=True
        ):
            raise ProfileSourceConflictError('source.admission_authorization_conflict')

    @staticmethod
    def _state(row):
        return ProfileFreshSourceStateV1(
            authorization=ProfileFreshSourceAuthorizationV1.model_validate(row['authorization']),
            status=row['status'],
            updated_at=row['updatedAt'],
            presence_proof=row.get('presenceProof'),
        )

    async def reserve_fresh_source(self, authorization, *, scope):
        authorization = self._authorization(authorization, scope)
        identity = 'cutover_' + source_key(scope, authorization.source_type, authorization.source_id)
        existing = await self.inbox.store.get(identity)
        if existing is not None:
            self._same(existing, authorization)
            if existing['status'] == 'ready':
                return self._state(existing)
        owner = await self._owner(authorization)
        proof = await self._absence(authorization, identity if existing else None)
        marker, _ = await self.inbox.store.insert(
            {
                '_id': identity,
                'recordKind': 'cutover',
                'revision': 0,
                'status': 'blocked',
                'admissionKind': 'fresh',
                'scope': scope.model_dump(by_alias=True),
                'authorization': authorization.model_dump(mode='json', by_alias=True),
                'ownerFenceProof': owner,
                'presenceProof': proof.model_dump(mode='json', by_alias=True),
                'updatedAt': self.inbox.now(),
            }
        )
        self._same(marker, authorization)
        if marker['status'] == 'ready':
            return self._state(marker)
        # The owner fence closes the external writer gap. The durable Profile
        # marker blocks subsequent legacy queue admission; scanning alone cannot.
        await self._owner(authorization)
        await self._absence(authorization, identity)
        return self._state(marker)

    async def authorize_fresh_source(self, authorization, *, scope):
        state = await self.reserve_fresh_source(authorization, scope=scope)
        if state.status == 'ready':
            return state
        authorization = state.authorization
        if authorization.source_type == 'health_report':
            resolver = self.inbox.document_source
            if resolver is None:
                raise ProfileSourceConflictError('source.document_receiver_not_ready')
            resolver = self.inbox._document_resolver(authorization)
            await resolver.validate_fresh_admission(authorization)
        identity = 'cutover_' + source_key(scope, authorization.source_type, authorization.source_id)
        await self._owner(authorization)
        proof = await self._absence(authorization, identity)

        def finish(row):
            self._same(row, authorization)
            row.update(
                status='ready', presenceProof=proof.model_dump(mode='json', by_alias=True), updatedAt=self.inbox.now()
            )

        await self.inbox._update(identity, finish)
        return self._state(await self.inbox.store.get(identity))
