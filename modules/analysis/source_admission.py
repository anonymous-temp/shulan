"""Fresh-only verified capture plans; existing-source governance is a separate command.

Zero workers means no executor holds authorization for formal source effects. A
publish=False preparation may continue, but a later formal write must enter the
same permanently fenced gateway. This is not a claim that every process stopped.
"""

from dataclasses import asdict

from modules.analysis.complete_source_contracts import DocumentCompleteSourceUnavailable
from modules.analysis.complete_source_contracts import DocumentContributionHandle
from modules.analysis.complete_source_contracts import DocumentSourceCapturePlan
from modules.analysis.report_publication_contracts import DocumentPreparedPublicationInput
from modules.analysis.snapshots import COMPLETE_SOURCE_CONTRACT
from modules.analysis.snapshots import DocumentScope
from modules.analysis.snapshots import canonical_json
from modules.analysis.snapshots import digest

OWNER_FENCE_CONTRACT = 'profile-owner-admission-fence.v1'
PRESENCE_CONTRACT = 'profile-source-presence.v1'
PLAN_REVISION = 'document-fresh-source-plan-v1'
PRESENCE_SECTIONS = {'reports', 'report_revisions', 'jobs', 'fact_contributions', 'publications'}


def _require(value, message):
    if not value:
        raise DocumentCompleteSourceUnavailable(message)


def _presence_proof(raw, scope, source_id):
    canonical = {key: raw[key] for key in ('contractVersion', 'scope', 'sourceType', 'sourceId', 'status', 'sections')}
    _require(
        canonical['contractVersion'] == PRESENCE_CONTRACT
        and canonical['scope'] == scope.as_dict()
        and canonical['sourceType'] == 'health_report'
        and canonical['sourceId'] == source_id,
        'Profile absence proof identity differs',
    )
    sections = canonical['sections']
    _require(
        canonical['status'] == 'absent'
        and len(sections) == len(PRESENCE_SECTIONS)
        and {row['kind'] for row in sections} == PRESENCE_SECTIONS
        and all(
            type(row['observedCount']) is int and row['observedCount'] == 0 and row['truncated'] is False
            for row in sections
        ),
        'complete all-history Profile absence is unavailable',
    )
    expected = digest(canonical)
    _require(
        raw['contentDigest'] == expected and raw['proofRef'] == 'profile_presence_' + expected,
        'Profile absence proof digest differs',
    )
    return canonical


class DocumentFreshSourceAdmission:
    """Commands fence new sources; readers replay fixed proofs without rescanning.

    No method accepts user-supplied quiescence/completeness flags. Only a single
    complete unpublished base is supported initially. Historical or already event-
    managed sources require a separately verified active-set transition command.
    """

    SOURCE_CONTRACT = COMPLETE_SOURCE_CONTRACT
    PREPARED_INPUT = DocumentPreparedPublicationInput

    def __init__(self, *, compatibility, contributions, ledger, records, history, profile_presence, profile_ready=None):
        self.compatibility = compatibility
        self.contributions = contributions
        self.ledger = ledger
        self.records = records
        self.history = history
        self.profile_presence = profile_presence
        self.profile_ready = profile_ready

    async def _prepared(self, scope, prepared):
        _require(
            isinstance(scope, DocumentScope) and isinstance(prepared, DocumentPreparedPublicationInput),
            'exact scope and original preparation checkpoint are required',
        )
        bundle = await self.compatibility.validate_prepared(
            scope=scope,
            understanding_snapshot_id=prepared.understanding_snapshot_ref,
            selection=prepared.selection,
            preparation_receipt=prepared.preparation_receipt,
        )
        _require(
            bundle.material.snapshot_id == prepared.material_snapshot_ref
            and bundle.facts.snapshot_id == prepared.facts_snapshot_ref,
            'preparation refs differ',
        )
        return bundle

    async def prepare_fresh_plan(self, *, scope, prepared, request_id, limit=1000):
        _require(
            isinstance(request_id, str) and request_id and request_id == request_id.strip(),
            'a fixed fresh command identity is required',
        )
        _require(type(limit) is int and 1 <= limit <= 1000, 'a bounded history scan is required')
        bundle = await self._prepared(scope, prepared)
        source_id = bundle.material.source_id
        manifest = {'scope': scope.as_dict(), 'prepared': prepared.as_dict()}
        manifest_ref = 'document_prepared_manifest_' + digest(manifest)
        request_ref = 'document_fresh_request_' + digest([scope.as_dict(), source_id, request_id])
        saved = await self.records.get('fresh_request', scope, request_ref)
        if saved is not None:
            _require(saved['preparedManifestRef'] == manifest_ref, 'fresh request changed its fixed inputs')
            await self.read_verified_plan(scope=scope, plan_ref=saved['planRef'])
            return saved['planRef']
        # Normal legacy sources must fail a fresh probe without any mutation.
        await self._absence(scope=scope, source_id=source_id, limit=limit)
        # Reject incomplete original quality before blocking an otherwise usable source.
        handle = await self.contributions.capture_base_contribution(
            scope=scope,
            understanding_snapshot_id=prepared.understanding_snapshot_ref,
        )
        await self.records.put('prepared_manifest', scope, manifest_ref, manifest)
        fence = await self.ledger.hold_fresh_admission(
            scope,
            source_id,
            request_id=request_id,
            prepared_manifest_ref=manifest_ref,
        )
        try:
            return await self._finish_fresh(
                scope=scope,
                bundle=bundle,
                handle=handle,
                fence=fence,
                manifest_ref=manifest_ref,
                request_ref=request_ref,
                limit=limit,
            )
        except BaseException as error:
            # The ledger atomically refuses rollback once an owner proof might
            # have escaped, including an unknown proof-storage acknowledgement.
            await self.ledger.abort_unissued_fresh_admission(
                scope,
                source_id,
                fence,
                reason=type(error).__name__,
            )
            raise

    async def _absence(self, *, scope, source_id, limit):
        history = await self.history.read_absence(scope=scope, source_id=source_id, limit=limit)
        _require(history['status'] == 'absent', 'Document formal history/intent absence is not proven')
        observed = await self.profile_presence.read_absence(scope=scope, source_id=source_id, limit=limit)
        canonical_presence = _presence_proof(observed, scope, source_id)
        return history, observed, canonical_presence

    async def _finish_fresh(self, *, scope, bundle, handle, fence, manifest_ref, request_ref, limit):
        source_id = bundle.material.source_id
        history, observed, canonical_presence = await self._absence(scope=scope, source_id=source_id, limit=limit)
        _require(await self.ledger.fresh_admission_held(scope, source_id, fence), 'fresh source fence was lost')
        owner = {
            'contractVersion': OWNER_FENCE_CONTRACT,
            'scope': scope.as_dict(),
            'sourceType': 'health_report',
            'sourceId': source_id,
            **fence,
            'formalHistoryAbsent': True,
            'legacyIntentsAbsent': True,
            'activeLegacyWriters': 0,
            'activeLegacyWorkers': 0,
            'legacyAdmissionBlocked': True,
        }
        await self.ledger.issue_fresh_owner_proof(scope, source_id, fence)
        await self.records.put('owner_fence', scope, fence['fenceId'], {'owner': owner, 'history': history})
        # Persist only canonical proof fields; readAt is not part of proof identity.
        await self.records.put('profile_presence', scope, observed['proofRef'], canonical_presence)
        _require(await self.ledger.fresh_admission_held(scope, source_id, fence), 'fresh source fence was lost')
        plan_input = {
            'revision': PLAN_REVISION,
            'preparedManifestRef': manifest_ref,
            'ownerFenceRef': fence['fenceId'],
            'profilePresenceProofRef': observed['proofRef'],
            'active': asdict(handle),
        }
        plan_ref = 'document_source_plan_' + digest([scope.as_dict(), source_id, plan_input])
        plan = DocumentSourceCapturePlan(
            scope=scope,
            source_id=source_id,
            source_revision=bundle.material.source_revision,
            active=(handle,),
            retired=(),
            legacy_state='absent',
            legacy_source_revision=None,
            fence_evidence_ref=fence['fenceId'],
            active_set_evidence_ref=plan_ref,
            _fence_json=canonical_json(
                {
                    'admissionKind': 'fresh',
                    'evidenceRef': fence['fenceId'],
                    'ownerFenceRef': {
                        'provider': 'document',
                        'contractVersion': OWNER_FENCE_CONTRACT,
                        'snapshotId': fence['fenceId'],
                    },
                    'ownerFenceDigest': digest(owner),
                    'profilePresenceProofRef': observed['proofRef'],
                    'profilePresenceProofDigest': observed['contentDigest'],
                }
            ),
        )
        await self.records.put('verified_plan', scope, plan_ref, {'input': plan_input, 'plan': asdict(plan)})
        await self.records.put(
            'fresh_request', scope, request_ref, {'preparedManifestRef': manifest_ref, 'planRef': plan_ref}
        )
        return plan_ref

    async def read_status(self, *, scope, source_id):
        source = await self.ledger.read_source(scope, source_id)
        if source is None:
            return {'mode': 'legacy', 'ownerProofIssued': False, 'fenceRef': None, 'lastAborted': None}
        return {
            'mode': source['mode'],
            'ownerProofIssued': source.get('freshOwnerProofIssued', False),
            'fenceRef': (source.get('freshAdmission') or {}).get('fenceId'),
            'lastAborted': source.get('freshAborted'),
        }

    async def recover_unissued(self, *, scope, prepared, request_id):
        """Explicit recovery for a lost gate ACK; never clears an issued proof."""
        bundle = await self._prepared(scope, prepared)
        manifest_ref = 'document_prepared_manifest_' + digest(
            {'scope': scope.as_dict(), 'prepared': prepared.as_dict()}
        )
        source = await self.ledger.read_source(scope, bundle.material.source_id)
        if source is None or source.get('freshAdmission') is None:
            return False
        expected = digest([scope.as_dict(), bundle.material.source_id, request_id, manifest_ref])
        _require(source.get('freshAdmissionRequest') == expected, 'recovery does not identify the held fresh request')
        return await self.ledger.abort_unissued_fresh_admission(
            scope,
            bundle.material.source_id,
            source['freshAdmission'],
            reason='explicit_unissued_recovery',
        )

    async def read_admission_fence(self, *, scope, snapshot_id):
        stored = await self.records.require('owner_fence', scope, snapshot_id)
        owner = stored['owner']
        _require(
            owner['scope'] == scope.as_dict()
            and owner['contractVersion'] == OWNER_FENCE_CONTRACT
            and owner['fenceId'] == snapshot_id
            and stored['history']['status'] == 'absent',
            'fixed owner fence proof differs',
        )
        manifest = await self.records.require('prepared_manifest', scope, owner['preparedManifestRef'])
        _require(
            owner['preparedManifestRef'] == 'document_prepared_manifest_' + digest(manifest)
            and manifest['scope'] == scope.as_dict(),
            'fixed prepared manifest differs',
        )
        bundle = await self._prepared(scope, self.PREPARED_INPUT.from_dict(manifest['prepared']))
        _require(bundle.material.source_id == owner['sourceId'], 'owner proof source differs from preparation')
        return owner

    async def is_admission_fence_held(self, *, scope, snapshot_id, content_digest):
        owner = await self.read_admission_fence(scope=scope, snapshot_id=snapshot_id)
        _require(digest(owner) == content_digest, 'owner fence digest differs')
        fence = {key: owner[key] for key in ('fenceId', 'fenceGeneration', 'preparedManifestRef', 'issuedAt')}
        return await self.ledger.fresh_admission_held(scope, owner['sourceId'], fence)

    async def read_verified_plan(self, *, scope, plan_ref):
        stored = await self.records.require('verified_plan', scope, plan_ref)
        raw = stored['plan']
        plan = DocumentSourceCapturePlan(
            **{
                **raw,
                'scope': DocumentScope(**raw['scope']),
                'active': tuple(DocumentContributionHandle(**row) for row in raw['active']),
                'retired': (),
            }
        )
        inputs = stored['input']
        _require(
            inputs['revision'] == PLAN_REVISION
            and not raw['retired']
            and plan.scope == scope
            and plan.active_set_evidence_ref == plan_ref
            and plan_ref == 'document_source_plan_' + digest([scope.as_dict(), plan.source_id, inputs]),
            'fixed fresh plan identity differs',
        )
        owner = await self.read_admission_fence(scope=scope, snapshot_id=inputs['ownerFenceRef'])
        proof = await self.records.require('profile_presence', scope, inputs['profilePresenceProofRef'])
        _presence_proof(
            {**proof, 'proofRef': inputs['profilePresenceProofRef'], 'contentDigest': digest(proof)},
            scope,
            plan.source_id,
        )
        expected_fence = {
            'admissionKind': 'fresh',
            'evidenceRef': owner['fenceId'],
            'ownerFenceRef': {
                'provider': 'document',
                'contractVersion': OWNER_FENCE_CONTRACT,
                'snapshotId': owner['fenceId'],
            },
            'ownerFenceDigest': digest(owner),
            'profilePresenceProofRef': inputs['profilePresenceProofRef'],
            'profilePresenceProofDigest': digest(proof),
        }
        _require(
            plan.fence == expected_fence
            and plan.active == (DocumentContributionHandle(**inputs['active']),)
            and owner['preparedManifestRef'] == inputs['preparedManifestRef'],
            'fixed plan proof bindings differ',
        )
        manifest = await self.records.require('prepared_manifest', scope, inputs['preparedManifestRef'])
        artifact, capture = await self.contributions._handle(scope, plan.active[0])
        _require(
            capture.payload['understandingSnapshotRef'] == manifest['prepared']['understandingSnapshotRef']
            and artifact.source_id == plan.source_id
            and artifact.source_revision == plan.source_revision,
            'the complete active base differs from the admitted preparation',
        )
        return plan

    async def activate(self, *, scope, plan_ref, source_reader, source_snapshot_id):
        """Enable event delivery only for the exact Profile-approved initial source."""
        _require(self.profile_ready is not None, 'the public Profile ready reader is required')
        plan = await self.read_verified_plan(scope=scope, plan_ref=plan_ref)
        source = await source_reader.read_source_observations(scope=scope, snapshot_id=source_snapshot_id)
        inventory = await source_reader.read_source_inventory(
            scope=scope,
            snapshot_id=source['payload']['coverage']['inventorySnapshotRef']['snapshotId'],
        )
        _require(
            source['sourceId'] == plan.source_id
            and source['contractVersion'] == self.SOURCE_CONTRACT
            and inventory['payload']['activeSetEvidenceRef'] == plan_ref,
            'complete source does not match its plan',
        )
        ready = await self.profile_ready.read_ready(scope=scope, source_id=plan.source_id)
        _require(ready is not None and ready['status'] == 'ready', 'Profile fresh admission is not ready')
        auth = ready['authorization']
        fence = plan.fence
        _require(
            auth['admissionKind'] == 'fresh'
            and auth['scope'] == scope.as_dict()
            and auth['sourceType'] == 'health_report'
            and auth['sourceId'] == plan.source_id
            and auth['ownerFenceRef'] == fence['ownerFenceRef']
            and auth['ownerFenceDigest'] == fence['ownerFenceDigest']
            and auth['presenceProofRef'] == fence['profilePresenceProofRef']
            and auth['presenceProofDigest'] == fence['profilePresenceProofDigest']
            and auth['materialContractVersion'] == self.SOURCE_CONTRACT
            and auth['materialSnapshotRef']
            == {'provider': 'document', 'contractVersion': self.SOURCE_CONTRACT, 'snapshotId': source_snapshot_id}
            and auth['materialContentDigest'] == digest(source),
            'Profile ready proof differs from the fixed source',
        )
        owner = await self.read_admission_fence(scope=scope, snapshot_id=fence['ownerFenceRef']['snapshotId'])
        held = {key: owner[key] for key in ('fenceId', 'fenceGeneration', 'preparedManifestRef', 'issuedAt')}
        await self.ledger.activate_fresh_admission(scope, plan.source_id, fence=held, ready_proof=auth)
