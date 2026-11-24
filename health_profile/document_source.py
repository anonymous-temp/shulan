"""Validate complete public Document inventories before source-wide governance."""

from copy import deepcopy

from pydantic import ValidationError

from .contracts import profile_content_digest
from .document_source_contracts import DOCUMENT_INVENTORY_CONTRACT
from .document_source_contracts import DOCUMENT_PROCESSING_REVISION
from .document_source_contracts import DOCUMENT_SOURCE_CONTRACT
from .document_source_contracts import DocumentCompleteSourceV1
from .document_source_contracts import DocumentSourceInventoryV1
from .document_source_contracts import ProfileDocumentSourceReadinessV1
from .ingestion import FactTypeRegistry
from .ingestion import HealthFactPolicy
from .report_ingestion import HealthReportCandidateMapper
from .source_material import ProfileSourceApplicationError
from .source_material import ProfileSourceMaterial


def _require(condition, code):
    if not condition:
        raise ProfileSourceApplicationError('source.document_' + code)


def _canonical(raw):
    import json

    _require(isinstance(raw, dict), 'contract_invalid')
    encoded = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    _require(len(encoded) <= 4_000_000, 'snapshot_too_large')
    return deepcopy(raw)


def _identity(value):
    return value.scope, value.source_type, value.source_id, value.source_revision


def validate_default_document_declaration(authorization):
    """Pure binding check for the still-active default entry, before marker writes."""
    if isinstance(authorization, dict):
        contract = authorization.get('materialContractVersion')
        revision = authorization.get('processingRevision')
        ref = authorization.get('materialSnapshotRef')
        provider = ref.get('provider') if isinstance(ref, dict) else None
        ref_contract = ref.get('contractVersion') if isinstance(ref, dict) else None
    else:
        contract = authorization.material_contract_version
        revision = authorization.processing_revision
        ref = authorization.material_snapshot_ref
        provider = ref.provider if ref is not None else None
        ref_contract = ref.contract_version if ref is not None else None
    _require(
        contract == DOCUMENT_SOURCE_CONTRACT
        and revision == DOCUMENT_PROCESSING_REVISION
        and provider == 'document'
        and ref_contract == contract,
        'contract_unsupported',
    )


class ProfileDocumentSourceResolver:
    """Only registered, fixed public reads; no capture, model, publish, or latest lookup."""

    VERSION = DOCUMENT_PROCESSING_REVISION

    def __init__(self, reader):
        self.reader = reader

    def registrations(self):
        """Install the exact publication and retraction readers as one supported pair."""
        return {
            ('document', DOCUMENT_SOURCE_CONTRACT): self,
            ('document', 'document.retraction.v1'): self,
        }

    def processing_manifest(self, contract_version):
        return {
            'implementationRevision': self.VERSION,
            'materialContract': contract_version,
            'mapperVersion': HealthReportCandidateMapper.VERSION,
            'policyVersion': HealthFactPolicy.VERSION,
            'factRegistryVersion': FactTypeRegistry.VERSION,
        }

    async def _complete(self, snapshot_ref, *, scope, source_id, content_digest):
        _require(
            snapshot_ref.provider == 'document' and snapshot_ref.contract_version == DOCUMENT_SOURCE_CONTRACT,
            'contract_unsupported',
        )
        raw = _canonical(await self.reader.read_source_observations(scope=scope, snapshot_id=snapshot_ref.snapshot_id))
        material = DocumentCompleteSourceV1.model_validate(raw)
        _require(material.scope == scope and material.source_id == source_id, 'scope_mismatch')
        _require(profile_content_digest(raw) == content_digest, 'content_digest_mismatch')
        _require(
            snapshot_ref.snapshot_id == 'document_source_snapshot_' + content_digest, 'snapshot_reference_mismatch'
        )
        coverage = material.payload.coverage
        _require(_identity(coverage) == _identity(material), 'coverage_identity_mismatch')
        ref = coverage.inventory_snapshot_ref
        _require(
            ref.provider == 'document' and ref.contract_version == DOCUMENT_INVENTORY_CONTRACT,
            'inventory_reference_invalid',
        )
        inventory_raw = _canonical(await self.reader.read_source_inventory(scope=scope, snapshot_id=ref.snapshot_id))
        inventory = DocumentSourceInventoryV1.model_validate(inventory_raw)
        _require(_identity(inventory) == _identity(material), 'inventory_identity_mismatch')
        _require(profile_content_digest(inventory_raw) == coverage.inventory_digest, 'inventory_digest_mismatch')
        _require(
            inventory.payload.legacy_source_revision == coverage.legacy_source_revision, 'legacy_revision_mismatch'
        )
        captured = inventory.payload.legacy_state == 'captured'
        _require(captured == (inventory.payload.legacy_source_revision is not None), 'legacy_coverage_unknown')
        contributions = material.payload.contributions
        ids = tuple(row.contribution_id for row in contributions)
        retired = inventory.payload.retired_contributions
        retired_ids = tuple(row.contribution_id for row in retired)
        _require(len(set(retired_ids)) == len(retired_ids), 'retired_reference_duplicate')
        _require(not set(ids).intersection(retired_ids), 'retired_contribution_active')
        _require(
            not {row.snapshot_id for row in contributions}.intersection(row.snapshot_id for row in retired),
            'retired_contribution_active',
        )
        _require(
            all(
                row.superseded_by_contribution_id is None or row.superseded_by_contribution_id in ids for row in retired
            ),
            'retirement_target_unproven',
        )
        _require(len(set(ids)) == len(ids) and ids == coverage.ordered_contribution_ids, 'contribution_order_mismatch')
        _require(
            tuple(row.reference() for row in contributions) == inventory.payload.contributions,
            'inventory_contributions_mismatch',
        )
        _require(
            len({row.snapshot_id for row in contributions}) == len(contributions), 'contribution_reference_duplicate'
        )
        _require(any(row.kind == 'base' for row in contributions), 'base_contribution_missing')
        _require(
            all(row.source_revision == material.source_revision for row in contributions), 'material_revision_mismatch'
        )
        _require(
            all(row.source_revision == material.source_revision for row in material.payload.material_snapshot_refs),
            'material_revision_mismatch',
        )
        _require(
            len({row.snapshot_id for row in material.payload.material_snapshot_refs})
            == len(material.payload.material_snapshot_refs),
            'material_reference_duplicate',
        )
        _require(
            captured or all(row.legacy_source_revision is None and row.kind == 'base' for row in contributions),
            'legacy_coverage_unknown',
        )
        _require(
            all(row.legacy_source_revision is not None for row in contributions if row.kind == 'legacy_report_source'),
            'legacy_revision_missing',
        )
        _require(sum(len(row.observations) for row in contributions) <= 1000, 'observation_limit_exceeded')
        groups = []
        for row, original in zip(contributions, raw['payload']['contributions'], strict=True):
            selected = row.implementation_selection
            allowed = (
                ('health_report', '1', 'analysis-health-report-v12')
                if row.kind == 'base'
                else ('report_source', '1', 'report-source-v16')
            )
            _require(
                (selected.profile_name, selected.contract_version, selected.implementation_revision) == allowed,
                'contribution_version_unsupported',
            )
            payload = {
                k: v
                for k, v in original.items()
                if k not in {'contractVersion', 'sourceRevision', 'snapshotId', 'contentDigest'}
            }
            canonical = {
                'scope': raw['scope'],
                'contractVersion': row.contract_version,
                'sourceType': 'health_report',
                'sourceId': source_id,
                'sourceRevision': row.source_revision,
                'payload': payload,
            }
            _require(profile_content_digest(canonical) == row.content_digest, 'contribution_digest_mismatch')
            pointers = tuple(item.observation_ref for item in row.observations)
            _require(len(set(pointers)) == len(pointers), 'observation_reference_duplicate')
            observations = []
            for item in row.observations:
                public = item.model_dump(mode='json', by_alias=True)
                # Null optional scope metadata is absence, not a scoped row. The
                # legacy mapper forbids root-date fallback when these keys exist.
                for name in ('sourceCategory', 'sourceObservationId', 'sourceSection'):
                    if public[name] is None:
                        del public[name]
                namespace = row.contribution_id.replace('~', '~0').replace('/', '~1')
                public['observationRef'] = '/contributions/' + namespace + item.observation_ref
                observations.append(public)
            groups.append(
                ProfileSourceMaterial(
                    scope,
                    'health_report',
                    source_id,
                    material.source_revision,
                    raw,
                    tuple(observations),
                    row.report_type,
                    row.report_time,
                )
            )
        readiness = ProfileDocumentSourceReadinessV1(
            scope=scope,
            source_id=source_id,
            source_revision=material.source_revision,
            snapshot_ref=snapshot_ref,
            content_digest=content_digest,
            inventory_snapshot_ref=ref,
            inventory_digest=coverage.inventory_digest,
            legacy_quiescence_evidence_ref=(
                inventory.payload.fence.evidence_ref if not hasattr(inventory.payload.fence, 'admission_kind') else None
            ),
            admission_kind='fresh' if hasattr(inventory.payload.fence, 'admission_kind') else 'legacy',
            admission_fence=inventory.payload.fence if hasattr(inventory.payload.fence, 'admission_kind') else None,
            legacy_source_revision=coverage.legacy_source_revision,
            contribution_ids=ids,
        )
        return ProfileSourceMaterial(
            scope,
            'health_report',
            source_id,
            material.source_revision,
            raw,
            tuple(item for group in groups for item in group.observations),
            contributions=tuple(groups),
            processing_selection=self.processing_manifest(material.contract_version),
        ), readiness

    async def inspect(self, snapshot_ref, *, scope, source_id, content_digest):
        """Pure readiness check. Success cannot itself authorize or manufacture a fence."""
        try:
            _, readiness = await self._complete(
                snapshot_ref, scope=scope, source_id=source_id, content_digest=content_digest
            )
            return readiness
        except (ValidationError, TypeError, ValueError, KeyError) as error:
            raise ProfileSourceApplicationError('source.document_contract_invalid') from error

    async def validate_cutover(self, authorization):
        validate_default_document_declaration(authorization)
        _require(authorization.source_type == 'health_report', 'source_kind_invalid')
        _require(authorization.material_contract_version == DOCUMENT_SOURCE_CONTRACT, 'complete_source_required')
        _require(authorization.processing_revision == self.VERSION, 'processing_version_unavailable')
        _require(
            authorization.material_snapshot_ref is not None and authorization.material_content_digest is not None,
            'cutover_material_required',
        )
        _require(
            authorization.legacy_source_intents_retired is True and bool(authorization.legacy_quiescence_evidence_ref),
            'quiescence_evidence_required',
        )
        ready = await self.inspect(
            authorization.material_snapshot_ref,
            scope=authorization.scope,
            source_id=authorization.source_id,
            content_digest=authorization.material_content_digest,
        )
        _require(
            ready.admission_kind == 'legacy'
            and ready.legacy_quiescence_evidence_ref == authorization.legacy_quiescence_evidence_ref,
            'quiescence_evidence_mismatch',
        )
        return ready

    async def validate_fresh_admission(self, authorization):
        validate_default_document_declaration(authorization)
        _require(authorization.source_type == 'health_report', 'source_kind_invalid')
        _require(authorization.material_contract_version == DOCUMENT_SOURCE_CONTRACT, 'complete_source_required')
        _require(authorization.processing_revision == self.VERSION, 'processing_version_unavailable')
        _require(
            authorization.material_snapshot_ref is not None and authorization.material_content_digest is not None,
            'cutover_material_required',
        )
        ready = await self.inspect(
            authorization.material_snapshot_ref,
            scope=authorization.scope,
            source_id=authorization.source_id,
            content_digest=authorization.material_content_digest,
        )
        fence = ready.admission_fence
        _require(ready.admission_kind == 'fresh' and fence is not None, 'fresh_fence_required')
        _require(ready.legacy_source_revision is None, 'fresh_legacy_history_present')
        _require(
            fence.owner_fence_ref == authorization.owner_fence_ref
            and fence.owner_fence_digest == authorization.owner_fence_digest
            and fence.profile_presence_proof_ref == authorization.presence_proof_ref
            and fence.profile_presence_proof_digest == authorization.presence_proof_digest,
            'fresh_fence_binding_mismatch',
        )
        return ready

    async def read_retraction(self, event):
        _require(event.snapshot_ref.contract_version == 'document.retraction.v1', 'contract_unsupported')
        raw = _canonical(
            await self.reader.read_retraction(scope=event.scope, snapshot_id=event.snapshot_ref.snapshot_id)
        )
        expected = {
            'scope': event.scope.model_dump(mode='json', by_alias=True),
            'contractVersion': 'document.retraction.v1',
            'sourceType': 'health_report',
            'sourceId': event.source_id,
            'sourceRevision': event.source_revision,
            'publicationRevision': event.publication_revision,
            'priorRevision': event.prior_revision,
            'eventKind': 'retract',
        }
        _require(raw == expected, 'retraction_identity_mismatch')
        _require(profile_content_digest(raw) == event.content_digest, 'content_digest_mismatch')
        _require(
            event.snapshot_ref.snapshot_id == 'document_snapshot_' + event.content_digest,
            'snapshot_reference_mismatch',
        )
        return ProfileSourceMaterial(
            event.scope,
            'health_report',
            event.source_id,
            event.source_revision,
            raw,
            processing_selection=self.processing_manifest(event.snapshot_ref.contract_version),
        )

    async def resolve(self, event):
        try:
            _require(
                event.source_type == 'health_report' and event.snapshot_ref.provider == 'document',
                'source_kind_invalid',
            )
            if event.event_kind == 'retract':
                return await self.read_retraction(event)
            material, ready = await self._complete(
                event.snapshot_ref, scope=event.scope, source_id=event.source_id, content_digest=event.content_digest
            )
            _require(material.source_revision == event.source_revision, 'material_revision_mismatch')
            _require(
                event.legacy_source_revision is None or event.legacy_source_revision == ready.legacy_source_revision,
                'legacy_revision_mismatch',
            )
            return material
        except (ValidationError, TypeError, ValueError, KeyError) as error:
            raise ProfileSourceApplicationError('source.document_contract_invalid') from error
