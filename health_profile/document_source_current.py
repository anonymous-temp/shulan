"""The one current Document resolver, with immutable source evidence."""

import json
from copy import deepcopy

from pydantic import TypeAdapter
from pydantic import ValidationError

from .basic_info import report_contribution_identity
from .basic_info_contracts import BasicInfoReportRetirement
from .contracts import profile_content_digest
from .document_source_contracts_v2 import DOCUMENT_INVENTORY_CONTRACT
from .document_source_contracts_v2 import DOCUMENT_PROCESSING_REVISION
from .document_source_contracts_v2 import DOCUMENT_SOURCE_CONTRACT
from .document_source_contracts_v2 import DocumentCompleteSource
from .document_source_contracts_v2 import DocumentFixedObservationRef
from .document_source_contracts_v2 import DocumentPatientIdentity
from .document_source_contracts_v2 import DocumentSourceInventory
from .document_source_contracts_v2 import ProfileDocumentSourceReadiness
from .ingestion import FactTypeRegistry
from .ingestion import HealthFactPolicy
from .report_ingestion import HealthReportCandidateMapper
from .source_material import ProfileSourceApplicationError
from .source_material import ProfileSourceMaterial


def _require(condition, code):
    if not condition:
        raise ProfileSourceApplicationError('source.document_' + code)


def _canonical(raw):
    _require(isinstance(raw, dict), 'contract_invalid')
    encoded = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    _require(len(encoded) <= 4_000_000, 'snapshot_too_large')
    return deepcopy(raw)


def _identity(value):
    return value.scope, value.source_type, value.source_id, value.source_revision


def _snapshot_reference(snapshot_id, content_digest):
    _require(snapshot_id == 'document_snapshot_' + content_digest, 'snapshot_reference_mismatch')


class ProfileCurrentDocumentSourceResolver:
    VERSION = DOCUMENT_PROCESSING_REVISION

    def __init__(self, reader):
        self.reader = reader

    def registrations(self):
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

    async def _complete(
        self, snapshot_ref, *, scope, source_id, content_digest, lifecycle_epoch=None, include_fixed_refs=False
    ):
        _require(
            snapshot_ref.provider == 'document' and snapshot_ref.contract_version == DOCUMENT_SOURCE_CONTRACT,
            'contract_unsupported',
        )
        raw = _canonical(await self.reader.read_source_observations(scope=scope, snapshot_id=snapshot_ref.snapshot_id))
        material = DocumentCompleteSource.model_validate(raw)
        _require(material.scope == scope and material.source_id == source_id, 'scope_mismatch')
        _require(profile_content_digest(raw) == content_digest, 'content_digest_mismatch')
        _require(
            snapshot_ref.snapshot_id == 'document_source_snapshot_' + content_digest, 'snapshot_reference_mismatch'
        )
        coverage = material.payload.coverage
        _require(_identity(coverage) == _identity(material), 'coverage_identity_mismatch')
        if lifecycle_epoch is not None:
            _require(
                type(lifecycle_epoch) is int and lifecycle_epoch == coverage.lifecycle_epoch,
                'lifecycle_epoch_mismatch',
            )
        ref = coverage.inventory_snapshot_ref
        _require(
            ref.provider == 'document' and ref.contract_version == DOCUMENT_INVENTORY_CONTRACT,
            'inventory_reference_invalid',
        )
        _snapshot_reference(ref.snapshot_id, coverage.inventory_digest)
        inventory_raw = _canonical(await self.reader.read_source_inventory(scope=scope, snapshot_id=ref.snapshot_id))
        inventory = DocumentSourceInventory.model_validate(inventory_raw)
        _require(_identity(inventory) == _identity(material), 'inventory_identity_mismatch')
        _require(profile_content_digest(inventory_raw) == coverage.inventory_digest, 'inventory_digest_mismatch')
        _require(inventory.payload.lifecycle_epoch == coverage.lifecycle_epoch, 'lifecycle_epoch_mismatch')
        contributions, retired = material.payload.contributions, inventory.payload.retired_contributions
        ids = tuple(row.contribution_id for row in contributions)
        retired_ids = tuple(row.contribution_id for row in retired)
        _require(bool(contributions or retired), 'active_set_unproven')
        _require(len(set(ids)) == len(ids) and ids == coverage.ordered_contribution_ids, 'contribution_order_mismatch')
        _require(contributions == inventory.payload.contributions, 'inventory_contributions_mismatch')
        _require(len(set(retired_ids)) == len(retired_ids), 'retired_reference_duplicate')
        _require(not set(ids).intersection(retired_ids), 'retired_contribution_active')
        _require(
            len({row.snapshot_id for row in contributions}) == len(contributions), 'contribution_reference_duplicate'
        )
        _require(len({row.snapshot_id for row in retired}) == len(retired), 'retired_reference_duplicate')
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
        _require(
            all(row.source_revision == material.source_revision for row in contributions), 'material_revision_mismatch'
        )
        _require(all(row.source_revision <= material.source_revision for row in retired), 'material_revision_mismatch')
        for row in (*contributions, *retired):
            _snapshot_reference(row.snapshot_id, row.content_digest)
        material_refs = material.payload.material_snapshot_refs
        _require(len({row.snapshot_id for row in material_refs}) == len(material_refs), 'material_reference_duplicate')
        for row in material_refs:
            _snapshot_reference(row.snapshot_id, row.content_digest)
            _require(row.source_revision <= material.source_revision, 'material_revision_mismatch')
        clinical_available = any(row.kind != 'basic_info' for row in (*contributions, *retired))
        _require(material.payload.clinical_available == clinical_available, 'clinical_coverage_mismatch')
        clinical = material.payload.clinical_contributions
        _require(
            tuple(row.reference() for row in clinical)
            == tuple(row for row in contributions if row.kind != 'basic_info'),
            'clinical_contributions_mismatch',
        )
        basic = tuple(field for row in contributions for field in getattr(row, 'basic_info', ()))
        _require(material.payload.basic_info == basic, 'basic_info_contributions_mismatch')
        _require(sum(len(row.observations) for row in clinical) <= 1000, 'observation_limit_exceeded')
        ordered_document_ids = tuple(dict.fromkeys(ref.document_id for ref in material_refs))
        document_ids = set(ordered_document_ids)
        routing_enabled = material.payload.quality.details.get('profileRouting') == 'report-patient.v1'
        signals = material.payload.quality.details.get('profileRoutingIdentitySignals', [])
        _require(isinstance(signals, list) and len(signals) <= 1000, 'patient_signal_limit_exceeded')
        signals = TypeAdapter(list[DocumentPatientIdentity]).validate_python(signals)
        _require(
            not signals or routing_enabled and all(row.source_document_id in document_ids for row in signals),
            'patient_signal_document_mismatch',
        )
        fixed_refs = {}
        read_refs = getattr(self.reader, 'read_fixed_observation_refs', None)
        if include_fixed_refs and read_refs is not None:
            fixed_refs = _canonical(await read_refs(scope=scope, snapshot_id=snapshot_ref.snapshot_id))
        groups = []
        for row, original in zip(clinical, raw['payload'].get('clinicalContributions', []), strict=True):
            canonical = {
                'scope': raw['scope'],
                'contractVersion': row.contract_version,
                'sourceType': 'health_report',
                'sourceId': source_id,
                'sourceRevision': row.source_revision,
                'payload': {
                    key: value
                    for key, value in original.items()
                    if key not in {'contractVersion', 'sourceRevision', 'snapshotId', 'contentDigest'}
                },
            }
            _require(profile_content_digest(canonical) == row.content_digest, 'contribution_digest_mismatch')
            pointers = tuple(item.observation_ref for item in row.observations)
            _require(len(set(pointers)) == len(pointers), 'observation_reference_duplicate')
            aliases = fixed_refs.pop(row.contribution_id, {})
            _require(isinstance(aliases, dict) and not (set(aliases) - set(pointers)), 'fixed_observation_ref_mismatch')
            observations = []
            for item in row.observations:
                _require(
                    item.physical_occurrence is None or item.physical_occurrence.document_id in document_ids,
                    'physical_occurrence_material_mismatch',
                )
                _require(
                    item.source_document_id is None or item.source_document_id in document_ids,
                    'observation_document_mismatch',
                )
                _require(
                    item.source_document_id is None
                    or item.physical_occurrence is None
                    or item.source_document_id == item.physical_occurrence.document_id,
                    'physical_occurrence_document_mismatch',
                )
                public = item.model_dump(mode='json', by_alias=True)
                for name in (
                    'sourceCategory',
                    'sourceObservationId',
                    'sourceSection',
                    'physicalOccurrence',
                    'verificationStatus',
                    'confirmationBasis',
                    'patientSlot',
                    'sourceDocumentId',
                ):
                    if public[name] is None:
                        del public[name]
                if item.observation_ref in aliases:
                    raw_refs = aliases[item.observation_ref]
                    _require(isinstance(raw_refs, list) and len(raw_refs) == 1, 'fixed_observation_ref_mismatch')
                    fixed_ref = DocumentFixedObservationRef.model_validate(raw_refs[0])
                    _require(
                        fixed_ref.document_id == item.source_document_id
                        and (
                            item.confirmation_basis == 'analysis_public'
                            and fixed_ref.observation_ref == item.observation_ref
                            or item.verification_status in {'verified_source', 'unconfirmed'}
                            and item.physical_occurrence is not None
                            and item.raw_value is not None
                        ),
                        'fixed_observation_ref_mismatch',
                    )
                    public['fixedAnalysisObservationRef'] = fixed_ref.model_dump(mode='json', by_alias=True)
                if include_fixed_refs and item.confirmation_basis == 'analysis_public':
                    _require('fixedAnalysisObservationRef' in public, 'fixed_observation_ref_mismatch')
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
                    lifecycle_epoch=coverage.lifecycle_epoch,
                )
            )
        _require(not fixed_refs, 'fixed_observation_ref_mismatch')
        _require(
            inventory.payload.fence.evidence_ref == inventory.payload.active_set_evidence_ref,
            'plan_reference_mismatch',
        )
        _require(
            all(field.source_document_id is None or field.source_document_id in document_ids for field in basic),
            'basic_info_document_mismatch',
        )
        bindings = {}
        for row in (*contributions, *retired):
            for field in row.basic_info:
                binding = (field.facts_snapshot_ref, field.interpretation_round, field.interpretation_revision)
                previous = bindings.setdefault(field.understanding_snapshot_ref, binding)
                _require(previous == binding, 'basic_info_binding_mismatch')
        readiness = ProfileDocumentSourceReadiness(
            scope=scope,
            source_id=source_id,
            source_revision=material.source_revision,
            lifecycle_epoch=coverage.lifecycle_epoch,
            snapshot_ref=snapshot_ref,
            content_digest=content_digest,
            inventory_snapshot_ref=ref,
            inventory_digest=coverage.inventory_digest,
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
            basic_info=tuple(deepcopy(raw['payload'].get('basicInfo', []))),
            basic_info_available=any(row.kind == 'basic_info' for row in (*contributions, *retired)),
            clinical_available=clinical_available,
            lifecycle_epoch=coverage.lifecycle_epoch,
            clinical_inventory_digest=profile_content_digest(raw['payload'].get('clinicalContributions', [])),
            routing_documents=ordered_document_ids if routing_enabled else (),
            routing_identity=tuple(row.model_dump(by_alias=True, exclude_none=True) for row in signals),
            basic_info_retirements=tuple(
                BasicInfoReportRetirement(
                    field=row.basic_info[0].field,
                    contribution_id=row.contribution_id,
                    identity_hash=report_contribution_identity(original['basicInfo'][0]),
                    retirement_evidence_ref=row.retirement_evidence_ref,
                )
                for row, original in zip(retired, inventory_raw['payload']['retiredContributions'], strict=True)
                if row.kind == 'basic_info'
                and row.retirement_kind == 'retracted'
                and row.superseded_by_contribution_id is None
            ),
        ), readiness

    async def inspect(self, snapshot_ref, *, scope, source_id, content_digest):
        try:
            _, readiness = await self._complete(
                snapshot_ref, scope=scope, source_id=source_id, content_digest=content_digest
            )
            return readiness
        except (ValidationError, TypeError, ValueError, KeyError) as error:
            raise ProfileSourceApplicationError('source.document_contract_invalid') from error

    async def _retraction(self, event, *, lifecycle_epoch=None):
        _require(event.snapshot_ref.contract_version == 'document.retraction.v1', 'contract_unsupported')
        raw = _canonical(
            await self.reader.read_retraction(scope=event.scope, snapshot_id=event.snapshot_ref.snapshot_id)
        )
        epoch = raw.get('lifecycleEpoch')
        _require(type(epoch) is int and epoch > 0, 'lifecycle_epoch_required')
        _require(lifecycle_epoch is None or lifecycle_epoch == epoch, 'lifecycle_epoch_mismatch')
        expected = {
            'scope': event.scope.model_dump(mode='json', by_alias=True),
            'contractVersion': 'document.retraction.v1',
            'sourceType': 'health_report',
            'sourceId': event.source_id,
            'sourceRevision': event.source_revision,
            'publicationRevision': event.publication_revision,
            'priorRevision': event.prior_revision,
            'eventKind': 'retract',
            'lifecycleEpoch': epoch,
        }
        _require(raw == expected, 'retraction_identity_mismatch')
        _require(profile_content_digest(raw) == event.content_digest, 'content_digest_mismatch')
        _snapshot_reference(event.snapshot_ref.snapshot_id, event.content_digest)
        return ProfileSourceMaterial(
            event.scope,
            'health_report',
            event.source_id,
            event.source_revision,
            raw,
            processing_selection=self.processing_manifest(event.snapshot_ref.contract_version),
            basic_info_available=True,
            lifecycle_epoch=epoch,
            clinical_inventory_digest=profile_content_digest([]),
        )

    async def resolve_fixed(self, event, processing_selection, *, lifecycle_epoch=None):
        if self.VERSION != DOCUMENT_PROCESSING_REVISION or processing_selection != self.processing_manifest(
            event.snapshot_ref.contract_version
        ):
            raise ProfileSourceApplicationError('source.processing_version_unavailable')
        return await self.resolve(event, lifecycle_epoch=lifecycle_epoch)

    async def resolve(self, event, *, lifecycle_epoch=None):
        if self.VERSION != DOCUMENT_PROCESSING_REVISION:
            raise ProfileSourceApplicationError('source.processing_version_unavailable')
        try:
            _require(
                event.source_type == 'health_report' and event.snapshot_ref.provider == 'document',
                'source_kind_invalid',
            )
            if event.event_kind == 'retract':
                return await self._retraction(event, lifecycle_epoch=lifecycle_epoch)
            material, _ = await self._complete(
                event.snapshot_ref,
                scope=event.scope,
                source_id=event.source_id,
                content_digest=event.content_digest,
                lifecycle_epoch=lifecycle_epoch,
                include_fixed_refs=True,
            )
            _require(material.source_revision == event.source_revision, 'material_revision_mismatch')
            return material
        except (ValidationError, TypeError, ValueError, KeyError) as error:
            raise ProfileSourceApplicationError('source.document_contract_invalid') from error
