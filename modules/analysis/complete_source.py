"""Capture neutral contributions from fixed Document artifacts, never consumer prose."""

from dataclasses import asdict

from modules.analysis.complete_source_contracts import DocumentCompleteSourceReceipt
from modules.analysis.complete_source_contracts import DocumentCompleteSourceUnavailable
from modules.analysis.complete_source_contracts import DocumentContributionHandle
from modules.analysis.complete_source_contracts import DocumentSourceCapturePlan
from modules.analysis.contracts import OutputSelection
from modules.analysis.report_publication_contracts import LEGACY_REPORT_SOURCE_SELECTION
from modules.analysis.snapshots import COMPLETE_SOURCE_CONTRACT
from modules.analysis.snapshots import RETRACTION_CONTRACT
from modules.analysis.snapshots import SOURCE_CAPTURE_PROOF_CONTRACT
from modules.analysis.snapshots import SOURCE_CONTRIBUTION_CONTRACT
from modules.analysis.snapshots import SOURCE_INVENTORY_CONTRACT
from modules.analysis.snapshots import DocumentScope
from modules.analysis.snapshots import DocumentSnapshot
from modules.analysis.snapshots import _observations
from modules.analysis.snapshots import canonical_json
from modules.analysis.snapshots import digest
from modules.analysis.understanding import UNDERSTANDING_SELECTION
from utils.report_source_observations import report_source_items

BASE_SELECTION = OutputSelection('health_report', '1', 'analysis-health-report-v12')
CAPTURE_REVISION = 'document-complete-source-capture-v1'


def _require(condition, message):
    if not condition:
        raise DocumentCompleteSourceUnavailable(message)


def _selection(value):
    return {
        'profileName': value.profile_name,
        'contractVersion': value.contract_version,
        'implementationRevision': value.implementation_revision,
    }


def _reference(snapshot):
    content = snapshot.payload
    return {
        key: value
        for key, value in {
            **content,
            'contractVersion': snapshot.contract_version,
            'snapshotId': snapshot.snapshot_id,
            'contentDigest': snapshot.content_digest,
            'sourceRevision': snapshot.source_revision,
        }.items()
        if key not in {'reportType', 'reportTime', 'observations'}
    }


def _material_ref(bundle):
    material = bundle.material
    return {
        'snapshotId': material.snapshot_id,
        'contractVersion': material.contract_version,
        'contentDigest': material.content_digest,
        'documentId': material.payload['documentId'],
        'sourceRevision': material.source_revision,
    }


class DocumentCompleteSourceService:
    """Capture commands persist artifacts; every read uses exact immutable refs.

    Admission is a separately injected provider. Callers cannot supply completeness
    booleans or an ad-hoc active set to turn into an accepted source snapshot.
    """

    def __init__(self, *, snapshots, understanding, report_publications, admission, managed_reports=None):
        self.snapshots = snapshots
        self.understanding = understanding
        self.report_publications = report_publications
        self.admission = admission
        self.managed_reports = managed_reports

    async def _bundle(self, scope, ref):
        _require(isinstance(scope, DocumentScope), 'explicit scope is required')
        bundle = await self.understanding.read_bundle(
            scope=scope,
            understanding_snapshot_id=ref,
            selection=UNDERSTANDING_SELECTION,
        )
        content = bundle.understanding.payload
        _require(bundle.material.scope == scope, 'fixed input scope mismatch')
        _require(content.get('inputVersionStatus') == 'known', 'unversioned input cannot prove a complete source')
        _require(
            content.get('interpretationRevision') == BASE_SELECTION.implementation_revision, 'base version unavailable'
        )
        _require(
            content.get('quality', {}).get('status') == 'complete', 'incomplete original interpretation cannot publish'
        )
        _require(
            _observations(content['interpretation']) == bundle.facts.payload['observations'],
            'fixed observations differ from the actual interpretation',
        )
        return bundle

    @staticmethod
    def _contribution(scope, source_id, source_revision, payload):
        observations = payload['observations']
        _require(len(observations) <= 1000, 'observation limit exceeded; truncation cannot prove completeness')
        _require(
            len({row['observationRef'] for row in observations}) == len(observations),
            'duplicate local observation pointer',
        )
        encoded = canonical_json(payload)
        _require(len(encoded.encode('utf-8')) <= 4_000_000, 'neutral contribution exceeds the supported byte bound')
        return DocumentSnapshot(scope, SOURCE_CONTRIBUTION_CONTRACT, source_id, source_revision, encoded)

    async def _base(self, scope, understanding_ref):
        bundle = await self._bundle(scope, understanding_ref)
        raw = bundle.understanding.payload['interpretation']
        snapshot = self._contribution(
            scope,
            bundle.material.source_id,
            bundle.material.source_revision,
            {
                'contributionId': 'base_' + digest(bundle.facts.snapshot_id),
                'kind': 'base',
                'legacySourceRevision': None,
                'implementationSelection': _selection(BASE_SELECTION),
                'reportType': raw.get('reportType', 'unknown'),
                'reportTime': raw.get('reportTime'),
                'observations': bundle.facts.payload['observations'],
            },
        )
        proof = {
            'captureRevision': CAPTURE_REVISION,
            'kind': 'base',
            'understandingSnapshotRef': understanding_ref,
            'contributionSnapshotRef': snapshot.snapshot_id,
            'materialSnapshotRefs': [_material_ref(bundle)],
            'originalQuality': bundle.understanding.payload['quality'],
        }
        return snapshot, proof

    async def _legacy(self, scope, receipt_ref):
        _require(self.report_publications is not None, 'legacy publication reader is unavailable')
        receipt = await self.report_publications.read_publication(scope=scope, receipt_ref=receipt_ref)
        _require(
            receipt.manifest.selection == LEGACY_REPORT_SOURCE_SELECTION, 'the real legacy v16 selection is required'
        )
        _require(receipt.processing_status == 'complete', 'incomplete Report output has no formal neutral contribution')
        _require(receipt.legacy_source_revision is not None, 'the real legacy source revision is required')
        projection = await self.report_publications.read_projection(
            scope=scope,
            projection_ref=receipt.projection_ref,
            selection=LEGACY_REPORT_SOURCE_SELECTION,
        )
        raw = projection.payload['projection']
        _require(
            raw.get('processing', {}).get('status', 'complete') == 'complete', 'incomplete legacy original quality'
        )
        bundles = [await self._bundle(scope, item.understanding_snapshot_ref) for item in receipt.manifest.inputs]
        _require(bundles[0].material.source_id == projection.source_id, 'legacy contribution canonical owner mismatch')
        _require(
            all(bundle.material.source_revision == projection.source_revision for bundle in bundles),
            'mixed real material revisions require a different supported contract',
        )
        execution = await self.snapshots.get(scope, projection.payload['executionSnapshotRef'])
        outcomes = execution.payload['outcomes']
        actual_ids = list(dict.fromkeys(bundle.material.payload['documentId'] for bundle in bundles))
        _require(
            [row['document_id'] for row in outcomes] == actual_ids, 'legacy execution does not match fixed materials'
        )
        observations = []
        for pointer, item in report_source_items(raw):
            observations.append(
                {
                    'observationRef': '/reportSource' + pointer,
                    'identityStatus': 'identity_unresolved',
                    'stableObservationId': None,
                    'label': item.get('itemName'),
                    'rawValue': item.get('resultValue'),
                    'unit': item.get('unit'),
                    'referenceRange': item.get('referenceRange'),
                    'flag': item.get('flag'),
                    'observedAt': item.get('observedAt'),
                    'kind': item.get('kind'),
                    'method': item.get('method'),
                    'specimen': item.get('specimen'),
                    'narrativeType': item.get('narrativeType'),
                    'sourceId': None,
                    'sourceCategory': item.get('sourceCategory'),
                    'evidence': [{'artifactRef': projection.snapshot_id, 'pointer': '/payload/projection' + pointer}],
                }
            )
        snapshot = self._contribution(
            scope,
            projection.source_id,
            projection.source_revision,
            {
                'contributionId': 'legacy_' + digest([projection.source_id, receipt.legacy_source_revision]),
                'kind': 'legacy_report_source',
                'legacySourceRevision': receipt.legacy_source_revision,
                'implementationSelection': _selection(LEGACY_REPORT_SOURCE_SELECTION),
                'reportType': outcomes[0]['report'].get('reportType', 'unknown'),
                'reportTime': outcomes[0]['report'].get('reportTime') if len(outcomes) == 1 else None,
                'observations': observations,
            },
        )
        proof = {
            'captureRevision': CAPTURE_REVISION,
            'kind': 'legacy_report_source',
            'publicationReceiptRef': receipt_ref,
            'projectionSnapshotRef': projection.snapshot_id,
            'contributionSnapshotRef': snapshot.snapshot_id,
            'materialSnapshotRefs': list(
                {bundle.material.snapshot_id: _material_ref(bundle) for bundle in bundles}.values()
            ),
            'originalQuality': {
                'processingStatus': receipt.processing_status,
                'projectionQuality': raw.get('processing'),
            },
        }
        return snapshot, proof

    async def _capture(self, scope, built):
        snapshot, payload = built
        proof = DocumentSnapshot(
            scope, SOURCE_CAPTURE_PROOF_CONTRACT, snapshot.source_id, snapshot.source_revision, canonical_json(payload)
        )
        await self.snapshots.put(snapshot)
        await self.snapshots.put(proof)
        return DocumentContributionHandle(snapshot.snapshot_id, proof.snapshot_id)

    async def capture_base_contribution(self, *, scope, understanding_snapshot_id):
        return await self._capture(scope, await self._base(scope, understanding_snapshot_id))

    async def _managed_base(self, scope, understanding_ref, transition_ref):
        snapshot, proof = await self._base(scope, understanding_ref)
        payload = snapshot.payload
        payload['contributionId'] = 'managed_base_' + digest([snapshot.snapshot_id, transition_ref])
        snapshot = self._contribution(scope, snapshot.source_id, snapshot.source_revision, payload)
        return snapshot, {
            **proof,
            'kind': 'managed_base',
            'transitionRef': transition_ref,
            'contributionSnapshotRef': snapshot.snapshot_id,
        }

    async def capture_managed_base_contribution(self, *, scope, understanding_snapshot_id, transition_ref):
        return await self._capture(scope, await self._managed_base(scope, understanding_snapshot_id, transition_ref))

    async def capture_legacy_report_contribution(self, *, scope, publication_receipt_ref):
        return await self._capture(scope, await self._legacy(scope, publication_receipt_ref))

    async def capture_managed_report_contribution(self, *, scope, archive_ref):
        _require(self.managed_reports is not None, 'the fixed managed Report reader is unavailable')
        return await self._capture(
            scope, await self.managed_reports.build_contribution(scope=scope, archive_ref=archive_ref)
        )

    async def _handle(self, scope, handle):
        _require(isinstance(handle, DocumentContributionHandle), 'fixed contribution/proof handle is required')
        snapshot = await self.snapshots.get(scope, handle.snapshot_ref)
        proof = await self.snapshots.get(scope, handle.proof_ref)
        _require(
            snapshot.contract_version == SOURCE_CONTRIBUTION_CONTRACT
            and proof.contract_version == SOURCE_CAPTURE_PROOF_CONTRACT,
            'the exact contribution and capture proof contracts are required',
        )
        content = proof.payload
        _require(content.get('captureRevision') == CAPTURE_REVISION, 'capture implementation is unavailable')
        if content.get('kind') == 'base':
            expected, expected_proof = await self._base(scope, content['understandingSnapshotRef'])
        elif content.get('kind') == 'managed_base':
            expected, expected_proof = await self._managed_base(
                scope, content['understandingSnapshotRef'], content['transitionRef']
            )
        elif content.get('kind') == 'legacy_report_source':
            expected, expected_proof = await self._legacy(scope, content['publicationReceiptRef'])
        elif content.get('kind') == 'managed_report_source':
            _require(self.managed_reports is not None, 'the fixed managed Report reader is unavailable')
            expected, expected_proof = await self.managed_reports.build_contribution(
                scope=scope, archive_ref=content['archiveRef']
            )
        else:
            raise DocumentCompleteSourceUnavailable('unsupported capture proof kind')
        _require(
            snapshot == expected
            and proof.payload == expected_proof
            and (proof.source_id, proof.source_revision) == (snapshot.source_id, snapshot.source_revision),
            'the contribution differs from its actual fixed input proof',
        )
        return snapshot, proof

    async def _source(self, scope, plan_ref):
        _require(self.admission is not None, 'a real verified source admission provider is required')
        plan = await self.admission.read_verified_plan(scope=scope, plan_ref=plan_ref)
        _require(isinstance(plan, DocumentSourceCapturePlan) and plan.scope == scope, 'verified plan scope is invalid')
        _require(plan.active_set_evidence_ref == plan_ref, 'the exact durable plan must be the active-set evidence')
        active = [await self._handle(scope, handle) for handle in plan.active]
        retired = [await self._handle(scope, item.handle) for item in plan.retired]
        _require(
            all(
                item.source_id == plan.source_id and item.source_revision == plan.source_revision for item, _ in active
            ),
            'active material identity mismatch',
        )
        _require(
            all(
                item.source_id == plan.source_id and item.source_revision <= plan.source_revision for item, _ in retired
            ),
            'retired material identity mismatch',
        )
        ids = [item.payload['contributionId'] for item, _ in active]
        retired_ids = [item.payload['contributionId'] for item, _ in retired]
        _require(
            len(set(ids)) == len(ids) and len(set(retired_ids)) == len(retired_ids), 'duplicate contribution identity'
        )
        _require(not set(ids).intersection(retired_ids), 'retired contributions cannot re-enter the active set')
        _require(
            any(item.payload['kind'] == 'base' for item, _ in active), 'the effective base contribution is missing'
        )
        if plan.legacy_state == 'absent':
            from modules.analysis.managed_source_contracts import DocumentManagedSourcePlan

            _require(
                (not retired or isinstance(plan, DocumentManagedSourcePlan))
                and all(
                    item.payload['kind'] == 'base' and item.payload['legacySourceRevision'] is None
                    for item, _ in active
                ),
                'fresh absence cannot cover historical contributions',
            )
        _require(
            sum(len(item.payload['observations']) for item, _ in active) <= 1000,
            'complete source observations exceed the supported bound',
        )
        materials = {}
        for _, proof in active:
            for ref in proof.payload['materialSnapshotRefs']:
                existing = materials.get(ref['documentId'])
                _require(existing is None or existing == ref, 'one document has conflicting fixed material inputs')
                materials[ref['documentId']] = ref
        audit = []
        for entry, (snapshot, _) in zip(plan.retired, retired, strict=True):
            _require(
                entry.superseded_by_contribution_id is None or entry.superseded_by_contribution_id in ids,
                'retirement replacement is not active',
            )
            audit.append(
                {
                    **_reference(snapshot),
                    'retirementKind': entry.retirement_kind,
                    'retirementEvidenceRef': entry.retirement_evidence_ref,
                    'supersededByContributionId': entry.superseded_by_contribution_id,
                }
            )
        inventory = DocumentSnapshot(
            scope,
            SOURCE_INVENTORY_CONTRACT,
            plan.source_id,
            plan.source_revision,
            canonical_json(
                {
                    'fence': plan.fence,
                    'activeSetEvidenceRef': plan.active_set_evidence_ref,
                    'legacyState': plan.legacy_state,
                    'legacySourceRevision': plan.legacy_source_revision,
                    'contributions': [_reference(item) for item, _ in active],
                    'retiredContributions': audit,
                }
            ),
        )
        source = DocumentSnapshot(
            scope,
            COMPLETE_SOURCE_CONTRACT,
            plan.source_id,
            plan.source_revision,
            canonical_json(
                {
                    'quality': {
                        'status': 'accepted',
                        'details': {'originalQualities': [proof.payload['originalQuality'] for _, proof in active]},
                    },
                    'materialSnapshotRefs': list(materials.values()),
                    'contributions': [
                        {
                            **_reference(item),
                            'reportType': item.payload['reportType'],
                            'reportTime': item.payload['reportTime'],
                            'observations': item.payload['observations'],
                        }
                        for item, _ in active
                    ],
                    'coverage': {
                        'contractVersion': 'document.source-coverage.v1',
                        'scope': scope.as_dict(),
                        'sourceType': 'health_report',
                        'sourceId': plan.source_id,
                        'sourceRevision': plan.source_revision,
                        'legacySourceRevision': plan.legacy_source_revision,
                        'inventorySnapshotRef': {
                            'provider': 'document',
                            'contractVersion': SOURCE_INVENTORY_CONTRACT,
                            'snapshotId': inventory.snapshot_id,
                        },
                        'inventoryDigest': inventory.content_digest,
                        'orderedContributionIds': ids,
                        'complete': True,
                    },
                }
            ),
        )
        for item in (source, inventory):
            _require(
                len(canonical_json(item.canonical).encode('utf-8')) <= 4_000_000,
                'complete source exceeds the supported byte bound',
            )
        proof = DocumentSnapshot(
            scope,
            SOURCE_CAPTURE_PROOF_CONTRACT,
            plan.source_id,
            plan.source_revision,
            canonical_json(
                {
                    'captureRevision': CAPTURE_REVISION,
                    'kind': 'complete_source',
                    'planRef': plan_ref,
                    'plan': asdict(plan),
                    'sourceSnapshotRef': source.snapshot_id,
                    'inventorySnapshotRef': inventory.snapshot_id,
                }
            ),
        )
        return source, inventory, proof

    async def capture_source(self, *, scope, verified_plan_ref):
        source, inventory, proof = await self._source(scope, verified_plan_ref)
        await self.snapshots.put(inventory)
        await self.snapshots.put(source)
        await self.snapshots.put(proof)
        return DocumentCompleteSourceReceipt(
            source.snapshot_id,
            source.content_digest,
            inventory.snapshot_id,
            inventory.content_digest,
            verified_plan_ref,
        )

    async def _read(self, scope, snapshot_id, contract):
        stored = await self.snapshots.get(scope, snapshot_id)
        _require(stored.contract_version == contract, 'the requested complete-source read contract differs')
        _require(self.admission is not None, 'the fixed capture binding reader is unavailable')
        if contract == COMPLETE_SOURCE_CONTRACT:
            ref = stored.payload['coverage']['inventorySnapshotRef']
            _require(
                ref['provider'] == 'document' and ref['contractVersion'] == SOURCE_INVENTORY_CONTRACT,
                'fixed inventory contract differs',
            )
            selected_inventory = await self.snapshots.get(scope, ref['snapshotId'])
        else:
            selected_inventory = stored
        _require(selected_inventory.contract_version == SOURCE_INVENTORY_CONTRACT, 'fixed inventory contract differs')
        source, inventory, proof = await self._source(scope, selected_inventory.payload['activeSetEvidenceRef'])
        expected = source if contract == COMPLETE_SOURCE_CONTRACT else inventory
        _require(
            stored == expected and selected_inventory == inventory,
            'the stored snapshot differs from the fixed verified plan',
        )
        persisted = await self.snapshots.get(scope, proof.snapshot_id)
        _require(persisted == proof, 'the immutable capture proof is unavailable or changed')
        return stored.canonical

    async def read_source_observations(self, *, scope, snapshot_id):
        return await self._read(scope, snapshot_id, COMPLETE_SOURCE_CONTRACT)

    async def read_source_inventory(self, *, scope, snapshot_id):
        return await self._read(scope, snapshot_id, SOURCE_INVENTORY_CONTRACT)

    async def read_retraction(self, *, scope, snapshot_id):
        snapshot = await self.snapshots.get(scope, snapshot_id)
        _require(snapshot.contract_version == RETRACTION_CONTRACT, 'the exact retraction contract is required')
        return snapshot.canonical
