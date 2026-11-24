"""Current neutral sources with independent patient-field contributions.

The admission reader supplies a durable, provider-verified active inventory.
This service verifies every fixed artifact; it never publishes Profile state.
"""

import json
from copy import deepcopy
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import replace

from modules.analysis.application.extraction import validated_document_time
from modules.analysis.basic_info_contracts import ANALYSIS_PUBLIC_CONFIRMATION
from modules.analysis.basic_info_contracts import CLINICAL_CONTENT_CONTRACT
from modules.analysis.basic_info_contracts import DocumentCurrentSourcePlan
from modules.analysis.complete_source import _material_ref
from modules.analysis.complete_source import _reference
from modules.analysis.complete_source import _require
from modules.analysis.complete_source import _selection
from modules.analysis.complete_source_contracts import DocumentCompleteSourceReceipt
from modules.analysis.complete_source_contracts import DocumentContributionHandle
from modules.analysis.complete_source_contracts import DocumentRetiredContribution
from modules.analysis.contracts import OutputSelection
from modules.analysis.current_occurrence import OCCURRENCE_PROJECTION_REVISIONS
from modules.analysis.current_occurrence import OCCURRENCE_REVISION
from modules.analysis.current_occurrence import physical_occurrences
from modules.analysis.current_report_contribution import report_occurrence_observations
from modules.analysis.profile_delivery import UNASSIGNED
from modules.analysis.profile_delivery import has_clinical_delivery
from modules.analysis.publication import PublicationConflictError
from modules.analysis.snapshots import COMPLETE_SOURCE_CONTRACT_V2
from modules.analysis.snapshots import CURRENT_REPORT_PROJECTION_CONTRACT
from modules.analysis.snapshots import RETRACTION_CONTRACT
from modules.analysis.snapshots import SOURCE_CAPTURE_PROOF_CONTRACT_V2
from modules.analysis.snapshots import SOURCE_CONTRIBUTION_CONTRACT_V2
from modules.analysis.snapshots import SOURCE_INVENTORY_CONTRACT_V2
from modules.analysis.snapshots import DocumentScope
from modules.analysis.snapshots import DocumentSnapshot
from modules.analysis.snapshots import _observations
from modules.analysis.snapshots import canonical_json
from modules.analysis.snapshots import digest

CAPTURE_REVISION = 'document-basic-info-capture-v3'


@dataclass(frozen=True, slots=True)
class DocumentReplacementProposal:
    """Input for durable admission, never an independently accepted publication."""

    active: tuple[DocumentContributionHandle, ...]
    retired: tuple[DocumentRetiredContribution, ...]
    _evidence_json: str

    @property
    def evidence(self):
        return json.loads(self._evidence_json)


class DocumentCurrentSourceArtifacts:
    """Pure fixed-input and inventory reconstruction, independent of command assembly."""

    def __init__(self, *, snapshots, understanding, report_projections=None):
        self.snapshots, self.understanding = snapshots, understanding
        self.report_projections = report_projections

    async def _snapshot_at(self, scope, snapshot_id, *, memo=None):
        key = ('snapshot', scope, snapshot_id)
        if memo is not None and key in memo:
            return memo[key]
        snapshot = await self.snapshots.get(scope, snapshot_id)
        if memo is not None:
            memo[key] = snapshot
        return snapshot

    async def _bundle_at(self, scope, understanding_ref, *, memo=None):
        key = ('bundle', scope, understanding_ref)
        if memo is not None and key in memo:
            return memo[key]
        if memo is None:
            return await self.understanding.read_bundle(scope=scope, understanding_snapshot_id=understanding_ref)
        bundle = await self.understanding._read_bundle(scope, understanding_ref, memo=memo)
        memo[key] = bundle
        return bundle

    async def _contributions(
        self,
        scope,
        understanding_ref,
        *,
        occurrence_projection_ref=None,
        confirmation_basis=None,
        epoch=None,
        memo=None,
    ):
        _require(isinstance(scope, DocumentScope), 'explicit scope is required')
        if confirmation_basis is not None:
            _require(
                confirmation_basis == ANALYSIS_PUBLIC_CONFIRMATION
                and occurrence_projection_ref is None
                and type(epoch) is int
                and epoch > 0,
                'default confirmation requires the original direct Analysis lifecycle',
            )
        key = ('contributions', scope, understanding_ref, occurrence_projection_ref, confirmation_basis, epoch)
        if memo is not None and key in memo:
            return memo[key]
        bundle = await self._bundle_at(scope, understanding_ref, memo=memo)
        facts, raw = bundle.facts.payload, bundle.understanding.payload['interpretation']
        _require(bundle.material.scope == scope, 'fixed input scope mismatch')
        fixed = {
            'understandingSnapshotRef': understanding_ref,
            'factsSnapshotRef': bundle.facts.snapshot_id,
            'interpretationRound': facts['interpretationRound'],
            'interpretationRevision': facts['extractorRevision'],
        }
        candidates = []
        document_id = bundle.material.payload['documentId']
        if has_clinical_delivery(facts):
            observations = [{**row, 'sourceDocumentId': document_id} for row in facts['observations']]
            if occurrence_projection_ref is not None:
                aliases = await self._fixed_analysis_occurrences(scope, bundle, occurrence_projection_ref, epoch, memo)
                observations = [
                    {**row, **aliases[row['observationRef']]} if row['observationRef'] in aliases else row
                    for row in observations
                ]
            if confirmation_basis == ANALYSIS_PUBLIC_CONFIRMATION:
                pointers = self._direct_observation_pointers(bundle)
                observations = [
                    self._confirmed_analysis_observation(row)
                    for row in observations
                    if row['observationRef'] in pointers or '/unresolvedItems/' in row['observationRef']
                ]
            candidates.append(('clinical', None, observations))
        candidates.extend(('basic_info', field, []) for field in facts['basicInfo'])
        result = []
        for kind, field, observations in candidates:
            identity = [understanding_ref, kind, field['field'] if field else None]
            if field and 'patientSlot' in field:
                identity.append(field['patientSlot'])
            capture_binding = {}
            if kind == 'clinical' and occurrence_projection_ref is not None:
                identity.extend((occurrence_projection_ref, epoch))
                capture_binding = {'occurrenceProjectionRef': occurrence_projection_ref, 'lifecycleEpoch': epoch}
            if kind == 'clinical' and confirmation_basis is not None:
                identity.extend((confirmation_basis, epoch))
                capture_binding = {'confirmationBasis': confirmation_basis, 'lifecycleEpoch': epoch}
            contribution_id = 'document_contribution_' + digest(identity)
            basic_info = (
                [{**field, **fixed, 'contributionId': contribution_id, 'sourceDocumentId': document_id}]
                if field
                else []
            )
            payload = {
                'contributionId': contribution_id,
                'kind': kind,
                'implementationSelection': _selection(OutputSelection(**bundle.understanding.payload['selection'])),
                'reportType': raw.get('reportType', 'unknown'),
                'reportTime': raw.get('reportTime'),
                'observations': observations,
                'basicInfo': basic_info,
                'clinicalContentContract': facts['clinicalContentContract'] if kind == 'clinical' else None,
                'clinicalContentDigest': facts['clinicalContentDigest'] if kind == 'clinical' else None,
            }
            _require(len(observations) <= 1000, 'observation bound exceeded; do not truncate')
            _require(
                len({r['observationRef'] for r in observations}) == len(observations), 'duplicate observation pointer'
            )
            snapshot = self._snapshot(bundle, SOURCE_CONTRIBUTION_CONTRACT_V2, payload)
            proof = self._snapshot(
                bundle,
                SOURCE_CAPTURE_PROOF_CONTRACT_V2,
                {
                    'captureRevision': CAPTURE_REVISION,
                    'kind': kind,
                    'field': field['field'] if field else None,
                    **({'patientSlot': field['patientSlot']} if field and 'patientSlot' in field else {}),
                    **fixed,
                    **capture_binding,
                    'contributionSnapshotRef': snapshot.snapshot_id,
                    'materialSnapshotRefs': [_material_ref(bundle)],
                    'originalQuality': raw.get('processing'),
                    'clinicalContentContract': payload['clinicalContentContract'],
                    'clinicalContentDigest': payload['clinicalContentDigest'],
                },
            )
            result.append((snapshot, proof))
        if memo is not None:
            memo[key] = result
        return result

    @staticmethod
    def _direct_observation_pointers(bundle):
        return {
            row['observationRef']
            for row in _observations(bundle.understanding.payload['interpretation'], normalize_display_date=True)
        }

    @staticmethod
    def _confirmed_analysis_observation(row):
        value = row['rawValue']
        if (
            '/unresolvedItems/' in row['observationRef']
            or not row['label']
            or value is None
            or (isinstance(value, str) and not value.strip())
        ):
            return row
        return {
            **row,
            'observedAt': validated_document_time(row['observedAt']),
            'verificationStatus': 'confirmed',
            'confirmationBasis': ANALYSIS_PUBLIC_CONFIRMATION,
        }

    async def _fixed_analysis_occurrences(self, scope, bundle, ref, epoch, memo):
        projection = await self._report_projection_at(scope, ref, lifecycle_epoch=epoch, memo=memo)
        fixed = projection.payload
        dependencies = fixed['dependencies']
        document = bundle.material.payload['documentId']
        _require(
            len(dependencies) == 1
            and dependencies[0]['documentId'] == document
            and dependencies[0]['materialSnapshotRef'] == bundle.material.snapshot_id
            and dependencies[0]['factsSnapshotRef'] == bundle.facts.snapshot_id
            and dependencies[0]['understandingSnapshotRef'] == bundle.understanding.snapshot_id
            and fixed['manifest']['interpretationProjectionRevision'] in OCCURRENCE_PROJECTION_REVISIONS,
            'the original occurrence proof requires the exact singleton preparation',
        )
        _, aliases = physical_occurrences(projection, (bundle,))
        rows = {row['observationRef']: row for row in self._original_report_observations(projection)}
        source_fields = ('label', 'rawValue', 'unit', 'kind', 'narrativeType', 'specimen', 'method')
        patient_slots = {}
        if fixed['manifest']['interpretationProjectionRevision'] == OCCURRENCE_REVISION:
            source_fields += ('referenceRange', 'flag')
            observations = {row['observationRef']: row for row in bundle.facts.payload['observations']}
            for (_, pointer), (report_pointer, _) in aliases.items():
                patient_slots.setdefault(report_pointer, set()).add(observations[pointer].get('patientSlot'))
        result = {}
        for (_, pointer), (report_pointer, proof) in aliases.items():
            if len(patient_slots.get(report_pointer, ())) > 1:
                continue
            row = rows[report_pointer]
            observed_at = validated_document_time(row['observedAt'])
            result[pointer] = {
                **{field: row[field] for field in source_fields},
                'observedAt': observed_at,
                'physicalOccurrence': proof,
                'verificationStatus': 'verified_source' if observed_at else 'unconfirmed',
            }
        return result

    async def _report_projection_at(self, scope, projection_ref, *, lifecycle_epoch, memo):
        _require(self.report_projections is not None, 'a verified current Report projection reader is required')
        key = ('report_projection', scope, projection_ref, lifecycle_epoch)
        if memo is not None and key in memo:
            return memo[key]
        projection = await self.report_projections.read(scope=scope, snapshot_id=projection_ref)
        fixed = projection.payload
        _require(
            projection.contract_version == CURRENT_REPORT_PROJECTION_CONTRACT
            and projection.scope == scope
            and projection.snapshot_id == projection_ref
            and fixed['manifest']['lifecycleEpoch'] == lifecycle_epoch,
            'current Report projection differs from the source lifecycle',
        )
        if memo is not None:
            memo[key] = projection
        return projection

    async def report_input_rounds(self, *, scope, projection_snapshot_ref, lifecycle_epoch, memo=None):
        projection = await self._report_projection_at(
            scope, projection_snapshot_ref, lifecycle_epoch=lifecycle_epoch, memo=memo
        )
        return {row['documentId']: row['interpretationRound'] for row in projection.payload['dependencies']}

    def _report_observations(self, projection, *, memo):
        key = ('report_observations', projection.scope, projection.snapshot_id)
        if memo is not None and key in memo:
            return memo[key]
        fixed = projection.payload
        observations = ()
        if fixed['processingStatus'] == 'complete':
            observations = self._original_report_observations(projection)
        if memo is not None:
            memo[key] = observations
        return observations

    @staticmethod
    def _original_report_observations(projection):
        fixed = projection.payload
        return tuple(
            report_occurrence_observations(
                fixed['projection'], projection_ref=projection.snapshot_id, dependencies=fixed['dependencies']
            )
        )

    async def _report_contribution(
        self, scope, projection_ref, *, lifecycle_epoch, primary_bundle=None, selected_ids=None, memo=None
    ):
        """Reconstruct the current capture from a verified projection without executing Report."""
        projection = await self._report_projection_at(scope, projection_ref, lifecycle_epoch=lifecycle_epoch, memo=memo)
        fixed = projection.payload
        dependencies = fixed['dependencies']
        first = dependencies[0]
        if primary_bundle is not None:
            _require(
                primary_bundle.material.scope == scope
                and first['materialSnapshotRef'] == primary_bundle.material.snapshot_id
                and first['factsSnapshotRef'] == primary_bundle.facts.snapshot_id
                and first['understandingSnapshotRef'] == primary_bundle.understanding.snapshot_id,
                'current Report projection is bound to a different primary preparation',
            )
        observations = self._report_observations(projection, memo=memo)
        if selected_ids is not None:
            included = set(selected_ids)
            observations = tuple(row for row in observations if row['stableObservationId'] in included)
            _require(
                tuple(row['stableObservationId'] for row in observations) == selected_ids,
                'Report capture must use exact original observation identities and order',
            )
        if not observations:
            return None
        selected_ids = tuple(row['stableObservationId'] for row in observations)
        key = ('report_contribution', scope, projection_ref, lifecycle_epoch, selected_ids)
        if memo is not None and key in memo:
            return memo[key]
        result = await self._materialize_report_contribution(
            scope, projection, fixed, dependencies, observations, selected_ids, memo=memo
        )
        if memo is not None:
            memo[key] = result
        return result

    async def _materialize_report_contribution(
        self, scope, projection, fixed, dependencies, observations, selected_ids, *, memo
    ):
        _require(len(observations) <= 1000, 'observation bound exceeded; do not truncate')
        bundles = [await self._bundle_at(scope, row['understandingSnapshotRef'], memo=memo) for row in dependencies]
        primary = bundles[0]
        physical, aliases = physical_occurrences(projection, bundles)
        patient_slots = {}
        for bundle in bundles:
            doc = bundle.material.payload['documentId']
            registered = bundle.facts.payload['observations']
            if any('patientSlot' in row for row in registered) or any(
                'patientSlot' in row for row in bundle.facts.payload['basicInfo']
            ):
                patient_slots[doc] = {}
            for row in registered:
                if row.get('patientSlot') and (ref := aliases.get((doc, row['observationRef']))):
                    patient_slots[doc].setdefault(ref[0], set()).add(row['patientSlot'])
        rows = []
        for row in observations:
            document_id = row['evidence'][0]['sourceDocumentId']
            observed_at = validated_document_time(row['observedAt'])
            slot, status = None, 'verified_source' if observed_at else 'unconfirmed'
            if occurrence := physical.get(row['observationRef']):
                row = {**row, 'observedAt': observed_at, 'physicalOccurrence': occurrence}
            if document_id in patient_slots:
                choices = patient_slots[document_id].get(row['observationRef'], set())
                slot = next(iter(choices)) if len(choices) == 1 else UNASSIGNED
            row = {**row, 'sourceDocumentId': document_id, 'verificationStatus': status}
            if slot is not None:
                row['patientSlot'] = slot
            rows.append(row)
        observations = tuple(rows)
        raw = primary.understanding.payload['interpretation']
        emitting_documents = {row['evidence'][0]['sourceDocumentId'] for row in observations}
        clinical_digest = digest(
            [
                (row['documentId'], row['clinicalContentDigest'])
                for row in dependencies
                if row['documentId'] in emitting_documents
            ]
        )
        identity = [
            scope.as_dict(),
            projection.source_id,
            projection.source_revision,
            fixed['manifest']['lifecycleEpoch'],
            [
                (row['documentId'], row['contentSha256'], row['clinicalContentDigest'], row['interpretationRound'])
                for row in dependencies
            ],
            selected_ids,
        ]
        if physical:
            identity.append(fixed['manifest']['interpretationProjectionRevision'])
        payload = {
            'contributionId': 'document_report_contribution_' + digest(identity),
            'kind': 'clinical',
            'implementationSelection': _selection(OutputSelection(**fixed['selection'])),
            'reportType': raw.get('reportType') or 'unknown',
            'reportTime': None,
            'observations': observations,
            'basicInfo': [],
            'clinicalContentContract': CLINICAL_CONTENT_CONTRACT,
            'clinicalContentDigest': clinical_digest,
        }
        snapshot = self._snapshot(primary, SOURCE_CONTRIBUTION_CONTRACT_V2, payload)
        proof = self._snapshot(
            primary,
            SOURCE_CAPTURE_PROOF_CONTRACT_V2,
            {
                'captureRevision': CAPTURE_REVISION,
                'kind': 'clinical',
                'field': None,
                'reportProjectionRef': projection.snapshot_id,
                'reportProjectionDigest': fixed['projectionDigest'],
                'reportTraceId': fixed['traceId'],
                'lifecycleEpoch': fixed['manifest']['lifecycleEpoch'],
                'contributionSnapshotRef': snapshot.snapshot_id,
                'materialSnapshotRefs': [_material_ref(bundle) for bundle in bundles],
                'originalQuality': {'status': fixed['processingStatus']},
                'clinicalContentContract': CLINICAL_CONTENT_CONTRACT,
                'clinicalContentDigest': clinical_digest,
                'clinicalDependencies': dependencies,
                'profileRoutingIdentitySignals': [
                    {
                        'sourceDocumentId': bundle.material.payload['documentId'],
                        'field': field['field'],
                        'value': field['value'],
                        **({'patientSlot': field['patientSlot']} if 'patientSlot' in field else {}),
                    }
                    for bundle in bundles
                    if bundle.material.payload['documentId'] in emitting_documents
                    for field in bundle.facts.payload['basicInfo']
                    if field['field'] in ('name', 'gender')
                ],
                'selectedObservationIds': selected_ids,
            },
        )
        return snapshot, proof

    @staticmethod
    def _snapshot(bundle, contract, payload):
        encoded = canonical_json(payload)
        _require(len(encoded.encode('utf-8')) <= 4_000_000, 'current artifact exceeds the supported byte bound')
        return DocumentSnapshot(
            bundle.material.scope, contract, bundle.material.source_id, bundle.material.source_revision, encoded
        )

    async def _handle(self, scope, handle, *, memo=None):
        _require(isinstance(handle, DocumentContributionHandle), 'fixed contribution/proof handle is required')
        key = ('handle', scope, handle)
        if memo is not None and key in memo:
            return memo[key]
        snapshot = await self._snapshot_at(scope, handle.snapshot_ref, memo=memo)
        proof = await self._snapshot_at(scope, handle.proof_ref, memo=memo)
        _require(
            snapshot.contract_version == SOURCE_CONTRIBUTION_CONTRACT_V2
            and proof.contract_version == SOURCE_CAPTURE_PROOF_CONTRACT_V2,
            'the current contribution and proof contracts are required',
        )
        _require(proof.payload.get('captureRevision') == CAPTURE_REVISION, 'capture implementation differs')
        if ref := proof.payload.get('reportProjectionRef'):
            expected = await self._report_contribution(
                scope,
                ref,
                lifecycle_epoch=proof.payload['lifecycleEpoch'],
                selected_ids=tuple(proof.payload['selectedObservationIds']),
                memo=memo,
            )
            _require((snapshot, proof) == expected, 'the Report contribution differs from its fixed projection')
        else:
            pairs = await self._contributions(
                scope,
                proof.payload['understandingSnapshotRef'],
                occurrence_projection_ref=proof.payload.get('occurrenceProjectionRef'),
                confirmation_basis=proof.payload.get('confirmationBasis'),
                epoch=proof.payload.get('lifecycleEpoch'),
                memo=memo,
            )
            _require((snapshot, proof) in pairs, 'the contribution differs from its actual fixed extraction')
        if memo is not None:
            memo[key] = snapshot, proof
        return snapshot, proof

    async def _source_from_plan(self, scope, plan_ref, plan, *, memo=None):
        _require(isinstance(plan, DocumentCurrentSourcePlan) and plan.scope == scope, 'verified plan scope is invalid')
        _require(plan.active_set_evidence_ref == plan_ref, 'the exact durable plan must be the active-set evidence')
        active = [await self._handle(scope, h, memo=memo) for h in plan.active]
        retired = [await self._handle(scope, r.handle, memo=memo) for r in plan.retired]
        _require(
            all(s.source_id == plan.source_id and s.source_revision == plan.source_revision for s, _ in active),
            'active material identity mismatch',
        )
        _require(
            all(s.source_id == plan.source_id and s.source_revision <= plan.source_revision for s, _ in retired),
            'retired material identity mismatch',
        )
        ids = [s.payload['contributionId'] for s, _ in active]
        retired_ids = [s.payload['contributionId'] for s, _ in retired]
        _require(
            len(set(ids)) == len(ids) and len(set(retired_ids)) == len(retired_ids), 'duplicate contribution identity'
        )
        _require(not set(ids).intersection(retired_ids), 'retired contributions cannot re-enter the active set')
        retired_report_occurrences = self._explicit_report_retirements(
            (entry, *pair) for entry, pair in zip(plan.retired, retired, strict=True)
        )
        _require(sum(len(s.payload['observations']) for s, _ in active) <= 1000, 'source observation bound exceeded')
        _require(
            all(
                p.payload.get('lifecycleEpoch') == plan.lifecycle_epoch
                for _, p in active + retired
                if {'reportProjectionRef', 'occurrenceProjectionRef', 'confirmationBasis'}.intersection(p.payload)
            ),
            'original occurrence or Report contribution differs from the verified source lifecycle',
        )
        clinical_inputs, report_occurrences = {}, set()
        for snapshot, proof in active:
            if 'reportProjectionRef' in proof.payload:
                occurrences = self._report_occurrences(snapshot)
                _require(not report_occurrences.intersection(occurrences), 'duplicate active Report source occurrence')
                _require(
                    retired_report_occurrences.isdisjoint(self._report_occurrence_dependencies(snapshot, proof)),
                    'a retired Report occurrence cannot re-enter with the same clinical input',
                )
                report_occurrences.update(occurrences)
            if snapshot.payload['kind'] == 'clinical' and not self._partial_clinical(proof):
                for doc_id, value in (await self._clinical_dependencies(scope, snapshot, proof)).items():
                    _require(
                        doc_id not in clinical_inputs or clinical_inputs[doc_id] == value,
                        'one document has conflicting active clinical inputs',
                    )
                    clinical_inputs[doc_id] = value
        materials, content_by_document = {}, {}
        for _, proof in active or retired:
            for ref in proof.payload['materialSnapshotRefs']:
                material = await self._snapshot_at(scope, ref['snapshotId'], memo=memo)
                content = material.payload['contentSha256']
                previous = content_by_document.get(ref['documentId'])
                _require(previous is None or previous == content, 'one document has conflicting original file bytes')
                content_by_document[ref['documentId']] = content
                # Round-specific metadata can produce distinct fixed material refs
                # for the same unchanged file. Carry every actual referenced input.
                materials[ref['snapshotId']] = ref
        _require(1 <= len(materials) <= 1000, 'source material reference bound exceeded')
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
        common = (scope, plan.source_id, plan.source_revision)
        inventory = self._source_snapshot(
            common,
            SOURCE_INVENTORY_CONTRACT_V2,
            {
                'fence': plan.fence,
                'lifecycleEpoch': plan.lifecycle_epoch,
                'activeSetEvidenceRef': plan_ref,
                'contributions': [_reference(s) for s, _ in active],
                'retiredContributions': audit,
            },
        )
        fields = [field for s, _ in active for field in s.payload.get('basicInfo', [])]
        source = self._source_snapshot(
            common,
            COMPLETE_SOURCE_CONTRACT_V2,
            {
                'quality': {
                    'status': 'accepted',
                    'details': {
                        'originalQualities': [p.payload['originalQuality'] for _, p in active],
                        'profileRouting': 'report-patient.v1',
                        'profileRoutingIdentitySignals': [
                            signal
                            for _, proof in active
                            for signal in proof.payload.get('profileRoutingIdentitySignals', ())
                        ],
                    },
                },
                'materialSnapshotRefs': list(materials.values()),
                'basicInfo': fields,
                'clinicalAvailable': any(s.payload['kind'] != 'basic_info' for s, _ in active + retired),
                'contributions': [_reference(s) for s, _ in active],
                'clinicalContributions': [
                    {
                        **_reference(s),
                        'reportType': s.payload['reportType'],
                        'reportTime': s.payload['reportTime'],
                        'observations': s.payload['observations'],
                    }
                    for s, _ in active
                    if s.payload['kind'] != 'basic_info'
                ],
                'coverage': {
                    'contractVersion': 'document.source-coverage.v2',
                    'lifecycleEpoch': plan.lifecycle_epoch,
                    'scope': scope.as_dict(),
                    'sourceType': 'health_report',
                    'sourceId': plan.source_id,
                    'sourceRevision': plan.source_revision,
                    'inventorySnapshotRef': {
                        'provider': 'document',
                        'contractVersion': SOURCE_INVENTORY_CONTRACT_V2,
                        'snapshotId': inventory.snapshot_id,
                    },
                    'inventoryDigest': inventory.content_digest,
                    'orderedContributionIds': ids,
                    'complete': True,
                },
            },
        )
        proof = self._source_snapshot(
            common,
            SOURCE_CAPTURE_PROOF_CONTRACT_V2,
            {
                'captureRevision': CAPTURE_REVISION,
                'kind': 'complete_source',
                'planRef': plan_ref,
                'plan': asdict(plan),
                'sourceSnapshotRef': source.snapshot_id,
                'inventorySnapshotRef': inventory.snapshot_id,
            },
        )
        return source, inventory, proof

    @staticmethod
    def _source_snapshot(common, contract, payload):
        scope, source_id, revision = common
        snapshot = DocumentSnapshot(scope, contract, source_id, revision, canonical_json(payload))
        _require(len(canonical_json(snapshot.canonical).encode('utf-8')) <= 4_000_000, 'source byte bound exceeded')
        return snapshot

    async def _clinical_dependencies(self, scope, snapshot, proof):
        """Read actual fixed clinical content, never compare a whole understanding ID."""
        kind = snapshot.payload['kind']
        if kind == 'basic_info':
            return {}
        if kind == 'clinical':
            fixed = proof.payload
            if 'reportProjectionRef' in fixed:
                documents = {row['evidence'][0]['sourceDocumentId'] for row in snapshot.payload['observations']}
                return {
                    row['documentId']: row['clinicalContentDigest']
                    for row in fixed['clinicalDependencies']
                    if row['documentId'] in documents
                }
            return {fixed['materialSnapshotRefs'][0]['documentId']: fixed['clinicalContentDigest']}
        raise ValueError('only current clinical and basic-info contributions are supported')

    @staticmethod
    def _partial_clinical(proof):
        quality = proof.payload.get('originalQuality')
        return not isinstance(quality, dict) or quality.get('status') != 'complete'

    @staticmethod
    def _delivery_rows(snapshot):
        rows = snapshot.payload['observations']
        for row in rows:
            if occurrence := row.get('physicalOccurrence'):
                occurrence.pop('proofSnapshotRef', None)
        return rows

    @staticmethod
    def _report_occurrences(snapshot):
        return {row['stableObservationId'] for row in snapshot.payload['observations']}

    @staticmethod
    def _contribution_rounds(proof):
        fixed = proof.payload
        if 'reportProjectionRef' in fixed:
            return {row['documentId']: row['interpretationRound'] for row in fixed['clinicalDependencies']}
        return {fixed['materialSnapshotRefs'][0]['documentId']: fixed['interpretationRound']}

    async def _verify_replacement_round(self, scope, prior, understanding_snapshot_id, *, memo):
        bundle = await self._bundle_at(scope, understanding_snapshot_id, memo=memo)
        material = bundle.material
        _require(
            material.scope == prior.scope == scope
            and material.source_id == prior.source_id
            and material.source_revision == prior.source_revision,
            'replacement fixed material identity differs',
        )
        document_id = material.payload['documentId']
        incoming_round = bundle.facts.payload['interpretationRound']
        # Retired contributions still prove that a later round entered this source.
        for handle in (*prior.active, *(row.handle for row in prior.retired)):
            _, proof = await self._handle(scope, handle, memo=memo)
            if self._contribution_rounds(proof).get(document_id, 0) > incoming_round:
                raise PublicationConflictError('replacement is from an earlier interpretation round')

    async def _derive_replacement(
        self,
        *,
        scope,
        prior,
        understanding_snapshot_id,
        incoming,
        occurrence_projection_ref=None,
        confirmation_basis=None,
        memo=None,
    ):
        """Pure fixed reconstruction, also used to verify a durable transition."""
        if memo is None:
            memo = {}
        await self._verify_replacement_round(scope, prior, understanding_snapshot_id, memo=memo)
        expected = tuple(
            DocumentContributionHandle(snapshot.snapshot_id, proof.snapshot_id)
            for snapshot, proof in await self._contributions(
                scope,
                understanding_snapshot_id,
                occurrence_projection_ref=occurrence_projection_ref,
                confirmation_basis=confirmation_basis,
                epoch=prior.lifecycle_epoch if occurrence_projection_ref or confirmation_basis else None,
                memo=memo,
            )
        )
        _require(tuple(incoming) == expected, 'replacement contributions differ from the exact fixed extraction')
        existing = [(h, *(await self._handle(scope, h, memo=memo))) for h in prior.active]
        additions = [(h, *(await self._handle(scope, h, memo=memo))) for h in incoming]
        _require(
            all(
                s.source_id == prior.source_id and s.source_revision == prior.source_revision
                for _, s, _ in existing + additions
            ),
            'replacement fixed material identity differs',
        )
        new_clinical = {}
        for _, snapshot, proof in additions:
            if snapshot.payload['kind'] == 'clinical':
                new_clinical.update(await self._clinical_dependencies(scope, snapshot, proof))
        complete = any(
            snapshot.payload['kind'] == 'clinical' and not self._partial_clinical(proof)
            for _, snapshot, proof in additions
        )
        old_clinical = {}
        for _, snapshot, proof in existing:
            if (
                snapshot.payload['kind'] == 'clinical'
                and 'reportProjectionRef' not in proof.payload
                and (not complete or not self._partial_clinical(proof))
            ):
                for doc_id, value in (await self._clinical_dependencies(scope, snapshot, proof)).items():
                    old_clinical.setdefault(doc_id, set()).add(value)
        changed = {doc_id for doc_id, value in new_clinical.items() if value not in old_clinical.get(doc_id, ())}
        active, retired, evidence = [], list(prior.retired), []
        for handle, snapshot, proof in existing:
            dependencies = await self._clinical_dependencies(scope, snapshot, proof)
            # A partial round contributes what it found. Missing rows from that
            # round cannot supersede earlier fixed or separately verified material.
            invalidated = complete and any(
                doc_id in new_clinical and (new_clinical[doc_id] != value or self._partial_clinical(proof))
                for doc_id, value in dependencies.items()
            )
            if snapshot.payload['kind'] == 'basic_info' or not invalidated:
                active.append(handle)
                continue
            detail = {
                'kind': 'fixed_clinical_input_superseded',
                'priorPlanRef': prior.active_set_evidence_ref,
                'retiredHandle': asdict(handle),
                'newUnderstandingSnapshotRef': understanding_snapshot_id,
                'oldClinicalDependencies': dependencies,
                'newClinicalDependencies': new_clinical,
            }
            ref = 'document_retirement_' + digest(detail)
            evidence.append({'evidenceRef': ref, **detail})
            retired.append(DocumentRetiredContribution(handle, 'corrected', ref))
        for handle, snapshot, proof in additions:
            if snapshot.payload['kind'] == 'clinical':
                if not changed:
                    continue
                if self._partial_clinical(proof) and any(
                    old.payload['kind'] == 'clinical'
                    and 'reportProjectionRef' not in old_proof.payload
                    and self._delivery_rows(old) == self._delivery_rows(snapshot)
                    for _, old, old_proof in existing
                ):
                    continue
            if handle not in active:
                active.append(handle)
        active_ids = {(await self._handle(scope, h, memo=memo))[0].payload['contributionId'] for h in active}
        retired = [
            replace(r, superseded_by_contribution_id=None) if r.superseded_by_contribution_id not in active_ids else r
            for r in retired
        ]
        _require(bool(active), 'replacement has no admissible field or clinical contribution')
        retired_ids = {
            (await self._handle(scope, row.handle, memo=memo))[0].payload['contributionId'] for row in retired
        }
        _require(not active_ids.intersection(retired_ids), 'retired contributions cannot re-enter the active set')
        return DocumentReplacementProposal(tuple(active), tuple(retired), canonical_json(evidence))

    @staticmethod
    def _report_occurrence_dependencies(snapshot, proof):
        dependencies = {
            row['documentId']: row['clinicalContentDigest'] for row in proof.payload['clinicalDependencies']
        }
        return {
            (row['stableObservationId'], dependencies[row['evidence'][0]['sourceDocumentId']])
            for row in snapshot.payload['observations']
        }

    def _explicit_report_retirements(self, retired):
        return {
            occurrence
            for retirement, snapshot, proof in retired
            if retirement.retirement_kind == 'retracted' and 'reportProjectionRef' in proof.payload
            for occurrence in self._report_occurrence_dependencies(snapshot, proof)
        }

    async def _managed_report_material(self, scope, prior, projection, *, memo):
        _require(
            scope == prior.scope
            and projection.source_id == prior.source_id
            and projection.source_revision == prior.source_revision,
            'Report projection belongs to a different fixed source',
        )
        dependencies = {row['documentId']: row['clinicalContentDigest'] for row in projection.payload['dependencies']}
        existing = [(h, *(await self._handle(scope, h, memo=memo))) for h in prior.active]
        retired = [(r, *(await self._handle(scope, r.handle, memo=memo))) for r in prior.retired]
        primary_document = next(iter(dependencies))
        _require(
            all(
                p.payload['materialSnapshotRefs'][0]['documentId'] == primary_document for _, _, p in existing + retired
            ),
            'Report projection must use the original primary document',
        )
        observations = self._report_observations(projection, memo=memo)
        blocked = self._explicit_report_retirements(retired)
        selected = tuple(
            row['stableObservationId']
            for row in observations
            if (row['stableObservationId'], dependencies[row['evidence'][0]['sourceDocumentId']]) not in blocked
        )
        _require(
            not observations or selected, 'retired contributions cannot re-enter: all Report occurrences are retired'
        )
        capture = await self._report_contribution(
            scope, projection.snapshot_id, lifecycle_epoch=prior.lifecycle_epoch, selected_ids=selected, memo=memo
        )
        return capture, existing, retired

    def _report_replacement_target(self, existing, group, replacement_id):
        reports = [entry for entry in existing if 'reportProjectionRef' in entry[2].payload]
        same_group = [entry for entry in reports if tuple(self._contribution_rounds(entry[2])) == group]
        _require(len(same_group) <= 1, 'duplicate active Report contributions for a document group')
        current = same_group[0] if same_group else None
        if replacement_id is None:
            return current
        target = next((entry for entry in reports if entry[1].payload['contributionId'] == replacement_id), None)
        if target is None or (current is not None and current != target):
            raise PublicationConflictError('Report replacement requires the exact active contribution for this group')
        old_group = tuple(self._contribution_rounds(target[2]))
        if old_group != group[: len(old_group)]:
            raise PublicationConflictError('Report replacement requires the same group or an ordered extension')
        return target

    def _verify_report_expansion(self, previous, snapshot, group, old_dependencies, new_dependencies):
        _, old_snapshot, old_proof = previous
        old_group = tuple(self._contribution_rounds(old_proof))
        if old_group == group:
            return
        unchanged = {
            row['stableObservationId']
            for row in old_snapshot.payload['observations']
            if old_dependencies[doc := row['evidence'][0]['sourceDocumentId']] == new_dependencies[doc]
        }
        if not unchanged.issubset(self._report_occurrences(snapshot)):
            raise PublicationConflictError('Report expansion must preserve unchanged original observations')
        added_documents = set(group[len(old_group) :])
        if not any(
            row['evidence'][0]['sourceDocumentId'] in added_documents for row in snapshot.payload['observations']
        ):
            raise PublicationConflictError('Report expansion requires an eligible observation from an added document')

    async def _derive_report_replacement(
        self, *, scope, prior, projection_snapshot_ref, incoming, replace_report_contribution_id=None, memo=None
    ):
        """Append a Report group or replace a fixed active group, including a prefix extension."""
        if memo is None:
            memo = {}
        projection = await self._report_projection_at(
            scope, projection_snapshot_ref, lifecycle_epoch=prior.lifecycle_epoch, memo=memo
        )
        capture, existing, retired = await self._managed_report_material(scope, prior, projection, memo=memo)
        if capture is None:
            _require(not incoming, 'ineligible Report projection cannot supply a contribution')
            _require(replace_report_contribution_id is None, 'replacement requires eligible Report clinical material')
            return DocumentReplacementProposal(prior.active, prior.retired, '[]')
        snapshot, proof = capture
        handle = DocumentContributionHandle(snapshot.snapshot_id, proof.snapshot_id)
        _require(tuple(incoming) == (handle,), 'Report contribution differs from the exact fixed projection')
        _require(await self._handle(scope, handle, memo=memo) == capture, 'fixed Report capture is unavailable')
        dependencies = projection.payload['dependencies']
        group = tuple(row['documentId'] for row in dependencies)
        previous = self._report_replacement_target(existing, group, replace_report_contribution_id)
        contribution_id = snapshot.payload['contributionId']
        _require(
            all(s.payload['contributionId'] != contribution_id for _, s, _ in retired),
            'retired contributions cannot re-enter the active set',
        )
        for _, _, old_proof in existing + retired:
            rounds = self._contribution_rounds(old_proof)
            if any(row['interpretationRound'] < rounds.get(row['documentId'], 0) for row in dependencies):
                raise PublicationConflictError('Report projection is from an earlier interpretation round')
        new_clinical = await self._clinical_dependencies(scope, snapshot, proof)
        if previous is not None:
            old_dependencies = {
                row['documentId']: row['clinicalContentDigest'] for row in previous[2].payload['clinicalDependencies']
            }
            new_dependencies = {row['documentId']: row['clinicalContentDigest'] for row in dependencies}
            if (
                old_dependencies == new_dependencies
                and previous[2].payload['selectedObservationIds'] == proof.payload['selectedObservationIds']
            ):
                return DocumentReplacementProposal(prior.active, prior.retired, '[]')
            if replace_report_contribution_id is None:
                raise PublicationConflictError('different clinical input requires explicit Report replacement')
            self._verify_report_expansion(previous, snapshot, group, old_dependencies, new_dependencies)
        new_occurrences = self._report_occurrences(snapshot)
        for old_handle, old_snapshot, old_proof in existing:
            if previous is not None and old_handle == previous[0]:
                continue
            old_clinical = await self._clinical_dependencies(scope, old_snapshot, old_proof)
            if not self._partial_clinical(old_proof) and any(
                doc in old_clinical and old_clinical[doc] != value for doc, value in new_clinical.items()
            ):
                raise PublicationConflictError('Report projection conflicts with active clinical input')
            if 'reportProjectionRef' in old_proof.payload and new_occurrences.intersection(
                self._report_occurrences(old_snapshot)
            ):
                raise PublicationConflictError('Report projection overlaps an active original occurrence')
        if previous is None:
            return DocumentReplacementProposal((*prior.active, handle), prior.retired, '[]')
        old_handle, old_snapshot, old_proof = previous
        detail = {
            'kind': 'fixed_report_projection_replaced',
            'priorPlanRef': prior.active_set_evidence_ref,
            'retiredHandle': asdict(old_handle),
            'newReportProjectionRef': projection_snapshot_ref,
            'oldClinicalDependencies': await self._clinical_dependencies(scope, old_snapshot, old_proof),
            'newClinicalDependencies': new_clinical,
        }
        evidence_ref = 'document_retirement_' + digest(detail)
        active = tuple(handle if existing_handle == old_handle else existing_handle for existing_handle in prior.active)
        retired_entries = tuple(
            replace(row, superseded_by_contribution_id=None)
            if row.superseded_by_contribution_id == replace_report_contribution_id
            else row
            for row in prior.retired
        )
        retired_entries += (DocumentRetiredContribution(old_handle, 'corrected', evidence_ref, contribution_id),)
        return DocumentReplacementProposal(
            active, retired_entries, canonical_json([{'evidenceRef': evidence_ref, **detail}])
        )

    async def read_retraction(self, *, scope, snapshot_id):
        snapshot = await self.snapshots.get(scope, snapshot_id)
        _require(snapshot.contract_version == RETRACTION_CONTRACT, 'the exact retraction contract is required')
        _require(
            type(snapshot.payload.get('lifecycleEpoch')) is int and snapshot.payload['lifecycleEpoch'] > 0,
            'current retraction requires its original fixed lifecycle',
        )
        return snapshot.canonical


class DocumentCurrentSourceReader(DocumentCurrentSourceArtifacts):
    """Read persisted plans and artifacts without needing a publisher or Profile inbox."""

    def __init__(self, *, snapshots, understanding, admission, report_projections=None):
        super().__init__(snapshots=snapshots, understanding=understanding, report_projections=report_projections)
        self.admission = admission

    async def read_retraction(self, *, scope, snapshot_id):
        raw = await super().read_retraction(scope=scope, snapshot_id=snapshot_id)
        original = await self.admission.read_original_source_plan(scope=scope, source_id=raw['sourceId'])
        _require(
            raw['sourceRevision'] == original.source_revision and raw['lifecycleEpoch'] == original.lifecycle_epoch,
            'retraction differs from its original fixed source lifecycle',
        )
        return raw

    async def _source(self, scope, plan_ref, *, memo=None):
        _require(self.admission is not None, 'a real verified source admission provider is required')
        if memo is None:
            memo = {}
        plan = await self.admission.read_verified_plan(scope=scope, plan_ref=plan_ref, memo=memo)
        return await self._source_from_plan(scope, plan_ref, plan, memo=memo)

    async def _read(self, scope, snapshot_id, contract, *, memo=None):
        if memo is None:
            memo = {}
        stored = await self._snapshot_at(scope, snapshot_id, memo=memo)
        _require(stored.contract_version == contract, 'the requested current source contract differs')
        selected_inventory = stored
        if contract == COMPLETE_SOURCE_CONTRACT_V2:
            ref = stored.payload['coverage']['inventorySnapshotRef']
            _require(
                ref['provider'] == 'document' and ref['contractVersion'] == SOURCE_INVENTORY_CONTRACT_V2,
                'fixed inventory contract differs',
            )
            selected_inventory = await self._snapshot_at(scope, ref['snapshotId'], memo=memo)
        _require(
            selected_inventory.contract_version == SOURCE_INVENTORY_CONTRACT_V2, 'fixed inventory contract differs'
        )
        source, inventory, proof = await self._source(
            scope, selected_inventory.payload['activeSetEvidenceRef'], memo=memo
        )
        expected = source if contract == COMPLETE_SOURCE_CONTRACT_V2 else inventory
        _require(
            stored == expected and selected_inventory == inventory, 'stored source differs from the fixed verified plan'
        )
        _require(
            await self._snapshot_at(scope, proof.snapshot_id, memo=memo) == proof, 'immutable capture proof differs'
        )
        return stored.canonical

    async def read_archive_material(self, *, scope, snapshot_id, document_id):
        """Return the fixed interpretation and active observations for one document."""
        memo = {}
        source = await self._read(scope, snapshot_id, COMPLETE_SOURCE_CONTRACT_V2, memo=memo)
        payload = source['payload']
        inventory = await self._snapshot_at(scope, payload['coverage']['inventorySnapshotRef']['snapshotId'], memo=memo)
        plan = await self.admission.read_verified_plan(
            scope=scope, plan_ref=inventory.payload['activeSetEvidenceRef'], memo=memo
        )
        selected = {row['contributionId']: row for row in payload['clinicalContributions']}
        source_digest = digest(source)
        bundles, observations, managed_bindings = {}, [], set()
        for handle in plan.active:
            snapshot, proof = await self._handle(scope, handle, memo=memo)
            contribution_id = snapshot.payload['contributionId']
            if contribution_id in selected:
                managed_bindings.update(await self._archive_managed_bindings(scope, snapshot, proof, memo=memo))
            fixed = proof.payload
            docs = tuple(row['documentId'] for row in fixed['materialSnapshotRefs'])
            if document_id not in docs:
                continue
            namespace = contribution_id.replace('~', '~0').replace('/', '~1')
            report_ref = fixed.get('reportProjectionRef')
            if report_ref:
                projection = await self._report_projection_at(
                    scope, report_ref, lifecycle_epoch=plan.lifecycle_epoch, memo=memo
                )
                reference = next(
                    row['understandingSnapshotRef']
                    for row in projection.payload['dependencies']
                    if row['documentId'] == document_id
                )
            else:
                reference = fixed['understandingSnapshotRef']
            bundle = await self._bundle_at(scope, reference, memo=memo)
            bundles.setdefault((bool(report_ref), -bundle.facts.payload['interpretationRound']), bundle)
            if contribution := selected.get(contribution_id):
                observations.extend(
                    {
                        **deepcopy(item),
                        'contributionSnapshotRef': snapshot.snapshot_id,
                        'contributionContentDigest': snapshot.content_digest,
                        'captureProofRef': proof.snapshot_id,
                        'materialSnapshotRefs': deepcopy(fixed['materialSnapshotRefs']),
                        'sourceSnapshotRef': snapshot_id,
                        'sourceContentDigest': source_digest,
                        'sourcePointer': '/contributions/' + namespace + item['observationRef'],
                        'contributionId': contribution_id,
                        'contributionKind': 'clinical',
                        'reportProjectionRef': report_ref,
                        'sourceRevision': source['sourceRevision'],
                        'documentId': item['evidence'][0]['sourceDocumentId'] if report_ref else document_id,
                        'documentIds': [item['evidence'][0]['sourceDocumentId'] if report_ref else document_id],
                    }
                    for item in contribution['observations']
                )
        for entry in plan.retired:
            if entry.retirement_kind != 'retracted':
                continue
            snapshot, proof = await self._handle(scope, entry.handle, memo=memo)
            managed_bindings.update(await self._archive_managed_bindings(scope, snapshot, proof, memo=memo))
        bundle = bundles[min(bundles)] if bundles else None
        return source, bundle, tuple(observations), tuple(sorted(managed_bindings))

    async def _archive_managed_bindings(self, scope, snapshot, proof, *, memo):
        if snapshot.payload['kind'] != 'clinical':
            return set()
        selected = {row['observationRef'] for row in snapshot.payload['observations']}
        rounds = self._contribution_rounds(proof)
        if not (ref := proof.payload.get('reportProjectionRef')):
            document_id, number = next(iter(rounds.items()))
            return {(document_id, number, pointer) for pointer in selected}
        projection = await self._report_projection_at(
            scope, ref, lifecycle_epoch=proof.payload['lifecycleEpoch'], memo=memo
        )
        key = ('archive_report_occurrence_aliases', scope, ref)
        if key not in memo:
            bundles = [
                await self._bundle_at(scope, row['understandingSnapshotRef'], memo=memo)
                for row in projection.payload['dependencies']
            ]
            _, memo[key] = physical_occurrences(projection, bundles)
        return {
            (document, rounds[document], analysis_ref)
            for (document, analysis_ref), (report_ref, _) in memo[key].items()
            if report_ref in selected
        }

    async def read_source_observations(self, *, scope, snapshot_id):
        return await self._read(scope, snapshot_id, COMPLETE_SOURCE_CONTRACT_V2)

    @staticmethod
    def _has_proven_original_location(row):
        return bool(
            row.get('physicalOccurrence')
            and (
                row['verificationStatus'] == 'verified_source'
                or (
                    row['verificationStatus'] == 'unconfirmed'
                    and row['observedAt'] is None
                    and row['rawValue'] is not None
                )
            )
        )

    async def read_fixed_observation_refs(self, *, scope, snapshot_id):
        """Expose exact cross-entry aliases without changing immutable publication digests."""
        memo = {}
        source = await self._read(scope, snapshot_id, COMPLETE_SOURCE_CONTRACT_V2, memo=memo)
        coverage = source['payload']['coverage']
        inventory = await self._snapshot_at(scope, coverage['inventorySnapshotRef']['snapshotId'], memo=memo)
        plan = await self.admission.read_verified_plan(
            scope=scope, plan_ref=inventory.payload['activeSetEvidenceRef'], memo=memo
        )
        result = {}
        for handle in plan.active:
            snapshot, proof = await self._handle(scope, handle, memo=memo)
            fixed = proof.payload
            observations = snapshot.payload['observations']
            by_pointer = {}
            if fixed.get('confirmationBasis') == ANALYSIS_PUBLIC_CONFIRMATION or 'occurrenceProjectionRef' in fixed:
                document_id, number = next(iter(self._contribution_rounds(proof).items()))
                for row in observations:
                    if row.get('confirmationBasis') == ANALYSIS_PUBLIC_CONFIRMATION or (
                        self._has_proven_original_location(row)
                    ):
                        pointer = row['observationRef']
                        by_pointer[pointer] = [
                            {'documentId': document_id, 'interpretationRound': number, 'observationRef': pointer}
                        ]
            elif ref := fixed.get('reportProjectionRef'):
                eligible = {row['observationRef'] for row in observations if self._has_proven_original_location(row)}
                if eligible:
                    projection = await self._report_projection_at(
                        scope, ref, lifecycle_epoch=plan.lifecycle_epoch, memo=memo
                    )
                    bundles = [
                        await self._bundle_at(scope, row['understandingSnapshotRef'], memo=memo)
                        for row in fixed['clinicalDependencies']
                    ]
                    _, aliases = physical_occurrences(projection, bundles)
                    rounds = self._contribution_rounds(proof)
                    retained = {
                        bundle.material.payload['documentId']: self._direct_observation_pointers(bundle)
                        for bundle in bundles
                    }
                    for (document_id, pointer), (report_pointer, _) in sorted(aliases.items()):
                        if report_pointer in eligible and pointer in retained[document_id]:
                            by_pointer.setdefault(report_pointer, []).append(
                                {
                                    'documentId': document_id,
                                    'interpretationRound': rounds[document_id],
                                    'observationRef': pointer,
                                }
                            )
            unique = {pointer: aliases for pointer, aliases in by_pointer.items() if len(aliases) == 1}
            if unique:
                result[snapshot.payload['contributionId']] = unique
        return result

    async def read_publication_material(self, *, scope, snapshot_id):
        memo = {}
        source = await self._read(scope, snapshot_id, COMPLETE_SOURCE_CONTRACT_V2, memo=memo)
        coverage = source['payload']['coverage']
        inventory = await self._snapshot_at(scope, coverage['inventorySnapshotRef']['snapshotId'], memo=memo)
        plan_ref = inventory.payload['activeSetEvidenceRef']
        transition = await self.admission.read_transition_binding(scope=scope, plan_ref=plan_ref, memo=memo)
        return source, {'planRef': plan_ref, 'lifecycleEpoch': coverage['lifecycleEpoch'], 'transition': transition}

    async def read_source_inventory(self, *, scope, snapshot_id):
        return await self._read(scope, snapshot_id, SOURCE_INVENTORY_CONTRACT_V2)


class DocumentCurrentContributionCapture:
    def __init__(self, artifacts):
        self.artifacts = artifacts

    async def _contributions(self, scope, understanding_snapshot_id, **kwargs):
        return await self.artifacts._contributions(scope, understanding_snapshot_id, **kwargs)

    async def _bundle_at(self, scope, understanding_snapshot_id, *, memo=None):
        return await self.artifacts._bundle_at(scope, understanding_snapshot_id, memo=memo)

    async def verify_complete_plan(self, scope, plan_ref, plan):
        await self.artifacts._source_from_plan(scope, plan_ref, plan, memo={})

    async def _report_contribution(self, scope, projection_ref, *, lifecycle_epoch, primary_bundle=None, memo=None):
        return await self.artifacts._report_contribution(
            scope, projection_ref, lifecycle_epoch=lifecycle_epoch, primary_bundle=primary_bundle, memo=memo
        )

    async def report_input_rounds(self, *, scope, projection_snapshot_ref, lifecycle_epoch, memo=None):
        return await self.artifacts.report_input_rounds(
            scope=scope, projection_snapshot_ref=projection_snapshot_ref, lifecycle_epoch=lifecycle_epoch, memo=memo
        )

    async def capture_report_contribution(
        self, *, scope, projection_snapshot_ref, lifecycle_epoch, primary_bundle=None, memo=None
    ):
        pair = await self._report_contribution(
            scope, projection_snapshot_ref, lifecycle_epoch=lifecycle_epoch, primary_bundle=primary_bundle, memo=memo
        )
        if pair is None:
            return None
        snapshot, proof = pair
        await self.artifacts.snapshots.put(snapshot)
        await self.artifacts.snapshots.put(proof)
        return DocumentContributionHandle(snapshot.snapshot_id, proof.snapshot_id)

    async def capture_contributions(self, *, scope, understanding_snapshot_id, **kwargs):
        pairs = await self._contributions(scope, understanding_snapshot_id, **kwargs)
        for snapshot, proof in pairs:
            await self.artifacts.snapshots.put(snapshot)
            await self.artifacts.snapshots.put(proof)
        return tuple(DocumentContributionHandle(s.snapshot_id, p.snapshot_id) for s, p in pairs)


class DocumentCurrentSourceService(DocumentCurrentSourceReader):
    """Explicit capture commands over the same fixed reader rules."""

    def __init__(self, *, snapshots, understanding, admission=None, report_projections=None):
        super().__init__(
            snapshots=snapshots,
            understanding=understanding,
            admission=admission,
            report_projections=report_projections,
        )

    async def capture_report_contribution(
        self, *, scope, projection_snapshot_ref, lifecycle_epoch, primary_bundle=None, memo=None
    ):
        return await DocumentCurrentContributionCapture(self).capture_report_contribution(
            scope=scope,
            projection_snapshot_ref=projection_snapshot_ref,
            lifecycle_epoch=lifecycle_epoch,
            primary_bundle=primary_bundle,
            memo=memo,
        )

    async def capture_managed_report_contribution(self, *, scope, prior, projection_snapshot_ref, memo=None):
        if memo is None:
            memo = {}
        projection = await self._report_projection_at(
            scope, projection_snapshot_ref, lifecycle_epoch=prior.lifecycle_epoch, memo=memo
        )
        pair, _, _ = await self._managed_report_material(scope, prior, projection, memo=memo)
        if pair is None:
            return None
        snapshot, proof = pair
        await self.snapshots.put(snapshot)
        await self.snapshots.put(proof)
        return DocumentContributionHandle(snapshot.snapshot_id, proof.snapshot_id)

    async def capture_contributions(self, *, scope, understanding_snapshot_id, **kwargs):
        return await DocumentCurrentContributionCapture(self).capture_contributions(
            scope=scope, understanding_snapshot_id=understanding_snapshot_id, **kwargs
        )

    async def capture_source(self, *, scope, verified_plan_ref):
        source, inventory, proof = await self._source(scope, verified_plan_ref)
        for item in (inventory, source, proof):
            await self.snapshots.put(item)
        return DocumentCompleteSourceReceipt(
            source.snapshot_id,
            source.content_digest,
            inventory.snapshot_id,
            inventory.content_digest,
            verified_plan_ref,
        )

    async def propose_replacement(self, *, scope, prior, understanding_snapshot_id):
        """Carry old fields; retire clinical/Report only for changed clinical inputs.

        The caller must durably admit this proposal through its normal lifecycle,
        source-version and command checks. This method grants no publication.
        """
        _require(isinstance(prior, DocumentCurrentSourcePlan) and prior.scope == scope, 'fixed prior plan is required')
        memo = {}
        await self._verify_replacement_round(scope, prior, understanding_snapshot_id, memo=memo)
        incoming = await self.capture_contributions(
            scope=scope, understanding_snapshot_id=understanding_snapshot_id, memo=memo
        )
        return await self._derive_replacement(
            scope=scope, prior=prior, understanding_snapshot_id=understanding_snapshot_id, incoming=incoming, memo=memo
        )
