"""Profile-owned interpretation of authorized immutable source snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .basic_info_contracts import BasicInfoReportRetirement
from .contracts import ProfileScope
from .contracts import profile_content_digest
from .report_ingestion import HealthReportCandidateMapper
from .report_ingestion import ReportMappingResult
from .report_ingestion import _report_observations
from .report_source import HealthReportSourceRecord
from .source_publications import source_key


class ProfileSourceApplicationError(RuntimeError):
    def __init__(self, code, *, retryable=False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class ProfileSourceMaterial:
    """Resolver output, not a public inbox payload or an authoritative fact set.

    snapshot is the provider's public canonical JSON, checked against content_digest.
    Observations retain original values; only Profile maps them to candidate types.
    """

    scope: ProfileScope
    source_type: str
    source_id: str
    source_revision: int
    snapshot: dict[str, Any]
    observations: tuple[dict[str, Any], ...] = ()
    report_type: str = 'lab'
    report_time: str | None = None
    contributions: tuple[ProfileSourceMaterial, ...] = ()
    processing_selection: dict[str, Any] | None = None
    basic_info: tuple[dict[str, Any], ...] = ()
    basic_info_available: bool = False
    clinical_available: bool = True
    lifecycle_epoch: int | None = None
    clinical_inventory_digest: str | None = None
    routing_documents: tuple[str, ...] = ()
    routing_identity: tuple[dict[str, Any], ...] = ()
    basic_info_retirements: tuple[BasicInfoReportRetirement, ...] = ()


def legacy_report_snapshot_id(scope, report_id, legacy_source_revision):
    return 'legacy_report_' + profile_content_digest([scope.model_dump(mode='json'), report_id, legacy_source_revision])


def legacy_report_material(record, *, source_revision):
    scope = ProfileScope(user_id=record.user_id, subject_id=record.subject_id)
    observations = tuple(
        {'observation_ref': pointer, **item}
        for pointer, item in _report_observations(record.result)
        if isinstance(item, dict)
    )
    snapshot = {
        'contractVersion': 'health-report-observations.v1',
        'scope': scope.model_dump(mode='json', by_alias=True),
        'sourceId': record.report_id,
        'sourceRevision': source_revision,
        'legacySourceRevision': record.source_revision,
        'reportType': record.result.get('reportType'),
        'reportTime': record.result.get('reportTime'),
        'observations': list(observations),
    }
    return ProfileSourceMaterial(
        scope=scope,
        source_type='health_report',
        source_id=record.report_id,
        source_revision=source_revision,
        snapshot=snapshot,
        observations=observations,
        report_type=record.result.get('reportType') or 'lab',
        report_time=record.result.get('reportTime'),
    )


class LegacyHealthReportSnapshotResolver:
    def __init__(self, source_service):
        self.source_service = source_service

    async def resolve(self, event):
        if event.source_type != 'health_report' or event.event_kind == 'retract':
            raise ProfileSourceApplicationError('source.contract_kind_unsupported')
        if event.legacy_source_revision is None:
            raise ProfileSourceApplicationError('source.legacy_revision_required')
        if event.snapshot_ref.snapshot_id != legacy_report_snapshot_id(
            event.scope, event.source_id, event.legacy_source_revision
        ):
            raise ProfileSourceApplicationError('source.snapshot_reference_mismatch')
        record = await self.source_service.get_owned_revision(
            event.source_id,
            event.legacy_source_revision,
            user_id=event.scope.user_id,
            subject_id=event.scope.subject_id,
        )
        if record is None:
            raise ProfileSourceApplicationError('source.snapshot_missing', retryable=True)
        return legacy_report_material(record, source_revision=event.source_revision)


def retraction_snapshot(event):
    return {
        'contractVersion': 'profile-source-retraction.v1',
        'scope': event.scope.model_dump(mode='json', by_alias=True),
        'sourceType': event.source_type,
        'sourceId': event.source_id,
        'sourceRevision': event.source_revision,
        'publicationRevision': event.publication_revision,
        'eventKind': 'retract',
    }


def retraction_snapshot_id(scope, source_type, source_id, publication_revision):
    return 'source_retraction_' + profile_content_digest(
        [source_key(scope, source_type, source_id), publication_revision]
    )


class ProfileRetractionSnapshotResolver:
    async def resolve(self, event):
        if event.event_kind != 'retract' or event.snapshot_ref.snapshot_id != retraction_snapshot_id(
            event.scope, event.source_type, event.source_id, event.publication_revision
        ):
            raise ProfileSourceApplicationError('source.retraction_reference_mismatch')
        return ProfileSourceMaterial(
            scope=event.scope,
            source_type=event.source_type,
            source_id=event.source_id,
            source_revision=event.source_revision,
            snapshot=retraction_snapshot(event),
        )


class ProfileSourceMaterialMapper:
    """Map complete source observations, never AI summaries or business report text."""

    def __init__(self):
        self.mapper = HealthReportCandidateMapper()

    def map(self, event, material, *, analysis_recorded_at=None):
        if event.source_type != 'health_report':
            raise ProfileSourceApplicationError('source.mapper_unsupported')
        if material.contributions:
            results = [
                self.map(event, group, analysis_recorded_at=analysis_recorded_at) for group in material.contributions
            ]
            candidates = tuple(row for rows, _ in results for row in rows)
            return candidates, ReportMappingResult(
                candidates,
                sum(result.source_item_count for _, result in results),
                sum(result.skipped_item_count for _, result in results),
                tuple(dict.fromkeys(code for _, result in results for code in result.reason_codes)),
            )
        observations = []
        references = set()
        for original in material.observations:
            reference = original.get('observation_ref', original.get('observationRef'))
            if not isinstance(reference, str) or not reference or reference in references:
                raise ProfileSourceApplicationError('source.observation_reference_invalid')
            references.add(reference)
            # Both the legacy bridge and Document FactsV1 expose original fields.
            item = {
                'itemName': original.get('label', original.get('itemName')),
                'resultValue': original.get('raw_value', original.get('rawValue', original.get('resultValue'))),
                'unit': original.get('unit'),
                'referenceRange': original.get('reference_range', original.get('referenceRange')),
                'flag': original.get('rawFlag', original.get('flag')),
                'observedAt': original.get('observed_at', original.get('observedAt')),
                'kind': original.get('kind'),
                'method': original.get('method'),
                'specimen': original.get('specimen'),
                'narrativeType': original.get('narrative_type', original.get('narrativeType')),
                'verificationStatus': original.get('verificationStatus'),
                'evidence': original.get('evidence'),
            }
            for name in ('sourceSection', 'sourceCategory', 'sourceObservationId', 'confirmationBasis'):
                if name in original:
                    item[name] = original[name]
            if reference.endswith(('/expansion/findings', '/expansion/diagnosis')):
                item['kind'] = 'narrative'
                item['narrativeType'] = 'diagnosis' if reference.endswith('/diagnosis') else 'finding'
            observations.append(item)
        record = HealthReportSourceRecord(
            report_id=event.source_id,
            source_revision=event.source_revision,
            user_id=event.scope.user_id,
            subject_id=event.scope.subject_id,
            source_fingerprint=event.content_digest,
            result={
                'reportType': material.report_type,
                'reportTime': material.report_time,
                'observations': observations,
            },
            created_at=event.occurred_at,
            updated_at=event.occurred_at,
            ingestion_status='pending',
            ingestion_job_id=None,
        )
        mapped = self.mapper.map(record, analysis_recorded_at=analysis_recorded_at)
        # Mapper order is not a stable cross-revision identity. References below
        # identify only the immutable publication snapshot.
        candidates = []
        for candidate in mapped.candidates:
            pointer = candidate.source.pointer or ''
            try:
                index = int(pointer.rsplit('/', 1)[-1])
            except ValueError as error:
                raise ProfileSourceApplicationError('source.observation_mapping_unsupported') from error
            if index >= len(material.observations):
                raise ProfileSourceApplicationError('source.observation_mapping_unsupported')
            reference = material.observations[index].get(
                'observation_ref', material.observations[index].get('observationRef')
            )
            candidates.append(
                candidate.model_copy(
                    update={
                        'candidate_id': 'source_observation_' + profile_content_digest(reference),
                        'source': candidate.source.model_copy(update={'pointer': reference}),
                    }
                )
            )
        return tuple(candidates), mapped
